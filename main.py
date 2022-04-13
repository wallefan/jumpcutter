import os
import subprocess
import tempfile
import wave
import audioop
import re
from nonlinear_time import NonLinearTime
import shutil
import glob

FRAME_LENGTH = 0.01
THRESHOLD = 0.7

SHOWINFO_RE = re.compile(r'^\[Parsed_showinfo_0.*] n:\s*(?P<n>\d+).*pts_time:(?P<time>[0-9.]+)')


def find_meaningful_audio(file, threshold):
    # extract the audio track from the file as mono wave sound
    proc = subprocess.Popen(
        ['ffmpeg', '-hide_banner', '-loglevel', 'warning',
         '-i', file, '-f', 'wav', '-ac', '1', '-'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    # for each FRAME_LENGTH seconds of audio, record the maximum peak-to-peak distance (audio_op.maxpp)
    # (aka the loudness of the sample)
    with wave.open(proc.stdout) as wf:
        wf: wave.Wave_read
        frame_size = int(FRAME_LENGTH * wf.getframerate())
        width = wf.getsampwidth()
        levels = []
        data = wf.readframes(frame_size)
        while data:
            levels.append(audioop.maxpp(data, width))
            data = wf.readframes(frame_size)

    # threshold is the percentage of audio frames that should be false, so 0.3 will mark approximately 70% of the audio
    # as meaningful
    # to implement this, we sort all the values we just extracted and pick the [threshold]th value of the sorted list
    # as our minimum level.
    s = sorted(levels)
    threshold = s[int(len(s) * threshold)]
    return [x > threshold for x in levels]


def add_padding(l, padding):
    state = l[0]
    i = 1
    while i < len(l):
        if l[i] != state:
            if not state:  # rising edge, add padding before
                for j in range(max(0, i - padding), i): l[j] = True
            else:  # falling edge, add padding after
                for j in range(i, min(i + padding, len(l))): l[j] = True
                i += padding
                if i >= len(l): break
            state = l[i]
        i += 1


def _jumpcut_audio(fin, fout, should_keep):
    proc = subprocess.Popen(
        ['ffmpeg', '-hide_banner', '-loglevel', 'warning',
         '-i', fin, '-f', 'wav', '-'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    with wave.open(proc.stdout) as fin, wave.open(fout, 'w') as fout:
        fin: wave.Wave_read
        fout: wave.Wave_write
        fout.setnchannels(fin.getnchannels())
        fout.setsampwidth(fin.getsampwidth())
        fout.setframerate(fin.getframerate())
        frame_size = int(FRAME_LENGTH * fin.getframerate())
        should_keep = iter(should_keep)
        data = fin.readframes(frame_size)
        while data:
            sk = next(should_keep, None)
            if sk is None:
                print('out of SK')
                sk = True
            if sk:
                # writeframesraw() is identical to writeframes() but does not update the header, which is important
                # if the output stream is unseekable.
                fout.writeframesraw(data)
            data = fin.readframes(frame_size)
        assert next(should_keep, None) is None


def find_runs(l):
    output = []
    current_state = l[0]
    run = 1
    for element in l[1:]:
        if element != current_state:
            output.append((current_state, run))
            current_state = element
            run = 1
        else:
            run += 1
    output.append((current_state, run))
    return output


def extract_frames(file, tempdir, start, length, framerate, start_number):
    subprocess.check_call(['ffmpeg',
                           '-ss', str(start * FRAME_LENGTH),
                           '-t', str(length * FRAME_LENGTH),
                           '-i', file,
                           '-r', str(framerate),
                           '-start_number', str(start_number),
                           os.path.join(tempdir, 'frame_%06d.png')])


def extract_all_frames(file, tempdir, framerate):
    subprocess.check_call(['ffmpeg', '-hide_banner',
                           '-i', file,
                           '-r', str(framerate),
                           os.path.join(tempdir, 'frame_%06d.png')])


def recombine_frames(outfile, tempdir, framerate):
    subprocess.check_call(['ffmpeg', '-hide_banner', '-framerate', str(framerate), '-i',
                           os.path.join(tempdir, 'frame_%06d.png'), outfile])


SOUND_SPEED = 1.0
SILENT_SPEED = 0.1


def audio_only(file, outfile, threshold=0.3, padding_time=0.05):
    print('Scanning audio...', end='')
    meaningful_parts = find_meaningful_audio(file, threshold)
    print('done (keep %f%%).' % (sum(meaningful_parts) * 100 / len(meaningful_parts)))
    print('Locating speech...', end='')
    add_padding(meaningful_parts, int(padding_time / FRAME_LENGTH))
    print('done (keep %f%%).' % (sum(meaningful_parts) * 100 / len(meaningful_parts)))
    print('Generating output...')
    _jumpcut_audio(file, outfile, meaningful_parts)
    print('done.')


def jumpcut(file, outfile, threshold=0.7, padding_time=0.02, sound_speed=1, silent_speed=0, subtitles=None):
    print('Scanning audio...', end='', flush=True)
    meaningful_parts = find_meaningful_audio(file, threshold)
    print('done (keep %f%%).' % (sum(meaningful_parts) * 100 / len(meaningful_parts)))
    print('Locating speech...', end='', flush=True)
    add_padding(meaningful_parts, int(padding_time / FRAME_LENGTH))
    print('done (keep %f%%).' % (sum(meaningful_parts) * 100 / len(meaningful_parts)))
    print('Building timeline...')
    timeline = []
    time = 0
    for is_meaningful, run_length in find_runs(meaningful_parts):
        timeline.append((time * FRAME_LENGTH, sound_speed if is_meaningful else silent_speed))
        time += run_length
    converter = NonLinearTime(timeline)

    # TODO implement audio time stretching
    assert sound_speed == 1 and silent_speed == 0

    with tempfile.TemporaryDirectory() as tmpdir:
        print('Processing subtitles...', end='', flush=True)
        if subtitles is None:
            rc = subprocess.run(['ffmpeg', '-i', file, os.path.join(tmpdir, 'subtitles.ass')], stderr=subprocess.DEVNULL).returncode
            if rc == 0:  # ffmpeg will return 1 if there is no subtitle stream.
                subtitles = os.path.join(tmpdir, 'subtitles.ass')
        elif not subtitles.endswith('.ass'):
            subprocess.check_call(['ffmpeg', '-i', subtitles, os.path.join(tmpdir, 'subtitles.ass')], stderr=subprocess.DEVNULL)
            subtitles = os.path.join(tmpdir, 'subtitles.ass') 
        if subtitles:
            with open(subtitles) as fin, open(os.path.join(tmpdir, 'converted.ass'), 'w') as fout:
                process_subtitles(fin, fout, converter)
            print('done.')
        else:
            print('nothing to do.')

        print('Processing audio...', end='', flush=True)
        # TODO process the audio more intelligently
        _jumpcut_audio(file, os.path.join(tmpdir, 'audio.wav'), meaningful_parts)
        print('done.')

        # Decrease this to 150 to 100 or 99 if your ffmpeg version doesn't like
        # the long expressions.
        pts_exprs = list(converter.generate_chunked_setpts_exprs(150))
        print('Video will be processed in %d chunks.' % len(pts_exprs))

        # process_video will print the "Processing..." messages on main's behalf
        video_filtergraph = process_video(file, tmpdir, pts_exprs)

        print('Encoding final result...')
        subprocess.check_call(['ffmpeg', '-y', '-hide_banner',
                                 '-filter_complex', video_filtergraph,
                                 '-i', 'audio.wav']  # wave file of audio (which we are piping into ffmpeg)
                                 + (['-i', 'converted.ass'] if subtitles else [])  # subtitle file
                                 +[os.path.abspath(outfile)],
                                stdin=subprocess.PIPE, cwd=tmpdir)
        print('done.')


class _FakeTemporaryDirectory:
    """Drop in replacement for tempfile.TemporaryDirectory that doesn't actually delete it when the program exits.
    Useful for debugging.
    """
    def __init__(self, dir):
        self.dir = dir
    def __enter__(self):
        # if os.path.exists(self.dir):
        #     import shutil
        #     shutil.rmtree(self.dir)
        # os.mkdir(self.dir)
        return self.dir
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def process_video(input_file, tempdir, pts_exprs):
    """
    Apply nonlinear speedup to the video portion.  We do this by altering the presentation timestamp (PTS) of each frame
    using ffmpeg's setpts filter, which takes an expression that ffmpeg will evaluate internally for every frame of the
    video, passing a different PTS parameter each time.  The best way to explain this expression is with an example.

    Suppose we have a video file with 10 seconds long, with sound from 3 seconds to 6 seconds.  We've been
    asked to speed up the sounded portions by a factor of 2 and the silent portions by a factor of 4.

    PTS is an integer, in units of TB seconds (TB, which stands for "time base", is another argument passed to the
    expression, and for most videos it has a value of 1/90,000).  In Python this expression would be:

    if PTS < 3/TB:
        return PTS * 0.25 # speedup by factor of 4 from 0 seconds to 3 seconds (this will result in 0.75 seconds of video)
    elif PTS < 6/TB:
        return (PTS - 3/TB) * 0.5 + 0.75/TB  # speedup by factor of 2 from 3 seconds to 6 seconds
                                             # (we subtract the amount of time since the start of the current window,
                                             #  multiply by the speedup factor, and add back the time we subtracted)
    else:
        return (PTS - 6/TB) * 0.25 + 3.75/TB

    etc.

    Translated to ffmpeg's expression format it becomes:

    if(lt(PTS,3/TB),PTS*0.25,if(lt(PTS,6/TB),(PTS-3/TB)*0.5+0.75/TB), ... )

    Due to the number of state transitions in the average video, these expressions quickly become so long and so complex
    that even after modifying and recompiling ffmpeg to increase the expression parser's arbitrary limit of 100 nested
    sets of parentheses to 10,000 (hence the use of ./private_ffmpeg, which is our modified version), we are still
    forced to break the expression into chunks because Linux won't let you pass a program an argument longer than 32,767
    characters.

    To work around this, we break the video into chunks, then return some information to our caller as to how to tell
    ffmpeg to stitch these chunks back together.

    :param input_file:
    :param tempdir:
    :param timestretch:
    :return:
    """
    # start and end are in seconds since the start of the video.
    # for an explanation of pts_expr please see timestretch.py
    files = []
    print('\x1b[s', end='')  # VT100 save cursor position
    for i, (start, end, pts_expr) in enumerate(pts_exprs):
        print('\x1b[uProcessing video (chunk %d of %d)...' % (i+1, len(pts_exprs)))
        subprocess.check_call(['/home/seanw/PycharmProjects/jumpcutter/private_ffmpeg', '-y',
                               '-loglevel', 'quiet',
                               '-stats',
                               # have 5 seconds of overlap between clips to avoid any possible missing spots
                               '-ss', str(start)] +
                               (['-to', str(end+5)] if end is not None else []) +
                               ['-i', input_file,
                               '-map', '0:v',  # ignore everything except the video track (no audio, no subtitles)
                               '-vf', 'setpts='+pts_expr,
                               os.path.join(tempdir, 'clip%02d.mkv' % (i+1))
                               ])
        files.append(os.path.join(tempdir, 'clip%02d.mkv' % (i+1)))
    total_file_count = i+1
    filters = ['movie=clip{0:02d}.mkv [v{0}]'.format(i+1) for i in range(total_file_count)]
    filters.append(' '.join('[v{}]'.format(i+1) for i in range(total_file_count))+' interleave=n='+str(total_file_count))
    return ';'.join(filters)



def process_frames(tempdir, timestretch:NonLinearTime, framerate):
    """Function to duplicate frames of video, used by a previous version of this program that was more similar
    in operation to carykh's version.  Unused in the current version.
    """
    input_frame = 1
    output_frame = 1
    while True:
        while timestretch.convert(input_frame/framerate) < output_frame/framerate:
            try:
                os.unlink(os.path.join(tempdir,'frame_%06d.png'%input_frame))
            except FileNotFoundError:
                return
            input_frame += 1
        try:
            new_name = os.path.join(tempdir, 'new_frame_%06d.png'%output_frame)
            os.rename(os.path.join(tempdir, 'frame_%06d.png'%input_frame), new_name)
        except FileNotFoundError:
            return
        # the input frame referenced has now been renamed, so we must increment input_frame to avoid it being referenced
        # again.
        input_frame += 1
        output_frame += 1
        while timestretch.convert(input_frame/framerate) > output_frame/framerate:
            # use symlinks here rather than copying the entire file *cough*carykh*cough*
            os.symlink(new_name, os.path.join(tempdir, 'new_frame_%06d.png'%output_frame))
            output_frame += 1


def process_subtitles(infile, outfile, converter: NonLinearTime):
    # what comes before the actual subtitle events in a Advanced SubStation (ASS) file is some formatting information
    # (what font to render the subtitles in, font size, color etc.)
    # Copy that information verbatim.
    for line in infile:
        outfile.write(line)
        if line.strip() == '[Events]':
            break
    start_idx = None
    end_idx = None
    num_parts = -1
    for line in infile:
        if not line or line.isspace():
            break
        type, sep, rest = line.partition(':')
        if not sep:
            outfile.write(type)
            continue
        parts = [x.strip() for x in rest.split(',', num_parts)]
        if type == 'Format':
            # the ASS format is somewhat flexible, and uses Format: rows
            # to define what CSV columns are used in the rest of the file.
            # Maintain these lines unchanged to the output.
            outfile.write(line)
            num_parts = len(parts) - 1
            start_idx = parts.index('Start')
            end_idx = parts.index('End')
        elif type == 'Dialogue':
            # rewrite the start and end time of the dialogue to match up with the changes we made to the video.
            start_time = _from_ass_time(parts[start_idx])
            start_time = converter.convert(start_time)
            parts[start_idx] = _to_ass_time(start_time)

            end_time = _from_ass_time(parts[end_idx])
            end_time = converter.convert(end_time)
            parts[end_idx] = _to_ass_time(end_time)
            outfile.write('Dialogue: {}\n'.format(','.join(parts)))
        else:
            # if we don't know what to do with a line, jut put it through verbatim.
            outfile.write(line)
    shutil.copyfileobj(infile, outfile)


def _from_ass_time(s):
    hrs, mins, secs = s.split(':')
    return int(hrs) * 3600 + int(mins) * 60 + float(secs)


def _to_ass_time(time):
    mins, secs = divmod(time, 60)
    hrs, mins = divmod(mins, 60)
    return '%d:%d:%.02f' % (hrs, mins, secs)

if __name__=='__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('input_file')
    parser.add_argument('output_file')
    parser.add_argument('--threshold', type=float, help='(between 0 and 1) the N% median of all audio levels '
                                                        'in the file will be used as the threshold for determining '
                                                        'whether audio is silent or not.  Default: 0.7',
                        default=0.7)
    parser.add_argument('--padding', type=float, help='add N seconds of padding to the start and end of every sounded '
                                                      'portion.  This helps reduce the number of state transitions. '
                                                      'Set to 0 to disable this feature.  Default: 0.05',
                        default=0.05)
    parser.add_argument('--silent-speed', type=float, help='the RECIPROCAL of the factor by which to speed up silent '
                                                           'portions of the video, i.e. 0.5 for a 2x speedup. '
                                                           'Passing 0 (the default) will remove the silent portions '
                                                           'altogether.', default=0)
    parser.add_argument('--sounded-speed', type=float, help='the RECIPROCAL of the factor by which to speed up '
                                                            'portions of the video where people are talking. '
                                                            'Default: 1', default=1)
    parser.add_argument('--subtitle-file', help='Alternate subtitle file (default is to use the one baked into the '
                                                'input file)', default=None)
    data = parser.parse_args()
    jumpcut(data.input_file, data.output_file, data.threshold, data.padding, data.sounded_speed, data.silent_speed,
            data.subtitle_file)
