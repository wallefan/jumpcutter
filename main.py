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


def jumpcut(file, outfile, threshold=0.7, padding_time=0.02, sound_speed=1, silent_speed=0):
    print('Scanning audio...', end='')
    meaningful_parts = find_meaningful_audio(file, threshold)
    print('done (keep %f%%).' % (sum(meaningful_parts) * 100 / len(meaningful_parts)))
    print('Locating speech...', end='')
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

    #with tempfile.TemporaryDirectory() as d:
    d = '/tmp/jumpcutter'
    os.mkdir(d)
    if True:
        print('Processing subtitles...', end='')
        subprocess.check_call(['ffmpeg', '-i', file, os.path.join(d, 'subtitles.ass')], stderr=subprocess.DEVNULL)
        with open(os.path.join(d, 'subtitles.ass')) as fin, open(os.path.join(d, 'converted.ass'), 'w') as fout:
            process_subtitles(fin, fout, converter)
        print('done.')

        print('Processing audio...', end='')
        # TODO process the audio more intelligently
        _jumpcut_audio(file, os.path.join(d, 'audio.wav'), meaningful_parts)
        print('done.')

        # print('Extracting frames...')
        # extract_all_frames(file, d, framerate)
        # print('done.')
        # 
        # print('Processing frames...')
        # process_frames(d, converter, framerate)
        # print('done.')

        ptsfilter = 'setpts='+converter.generate_setpts_expr()
        print('pts filter bytes:', len(ptsfilter))

        print('Encoding final result...')
        # TODO make subtitles optional
        subprocess.check_call(['/home/seanw/PycharmProjects/jumpcutter/private_ffmpeg', '-hide_banner',
                                 '-i', file,
                                 '-i', os.path.join(d, 'audio.wav'),  # wave file of audio (which we are piping into ffmpeg)
                                 '-i', os.path.join(d, 'converted.ass'),  # subtitle file
                                 '-r', str(framerate),  # set framerate of the video data
                                 '-map', '0:v', '-map', '1:a', '-map', 2,
                                 '-vf', ptsfilter,
                                 outfile],
                                stdin=subprocess.PIPE)
        print('done.')


def process_video(input_file, tempdir, timestretch:NonLinearTime):
    # start and end are in seconds since the start of the video.
    # for an explanation of pts_expr please see timestretch.py
    files = []
    for i, (start, end, pts_expr) in enumerate(timestretch.generate_chunked_setpts_exprs(10_000)):
        subprocess.check_call(['./private_ffmpeg', '-y',
                               '-loglevel','quiet',
                               '-stats',
                               # have 5 seconds of overlap between clips to avoid any possible missing spots
                               '-ss', str(start)] +
                               ['-to', str(end+5)] if end is not None else [] +
                               ['-i', input_file,
                               '-map', '0:v',  # ignore everything except the video track (no audio, no subtitles)
                               #'-vf', 'trim=start=%.2f:end=%.2f' % (start, end+5),
                               '-vf', 'setpts='+pts_expr,
                               os.path.join(tempdir, 'clip%02d.mkv' % (i+1))
                               ])
        files.append(os.path.join(tempdir, 'clip%02d.mkv' % (i+1)))
    return files


def process_frames(tempdir, timestretch:NonLinearTime, framerate):
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
            # maintain these lines unchanged to the output.
            outfile.write(line)
            num_parts = len(parts) - 1
            start_idx = parts.index('Start')
            end_idx = parts.index('End')
        elif type == 'Dialogue':
            # 14-year-old me would be cringing so hard at this code
            start_time = _from_ass_time(parts[start_idx])
            start_time = converter.convert(start_time)
            parts[start_idx] = _to_ass_time(start_time)

            end_time = _from_ass_time(parts[end_idx])
            end_time = converter.convert(end_time)
            parts[end_idx] = _to_ass_time(end_time)
            outfile.write('Dialogue: {}\n'.format(','.join(parts)))
        else:
            outfile.write(line)
    shutil.copyfileobj(infile, outfile)


def _from_ass_time(s):
    hrs, mins, secs = s.split(':')
    return int(hrs) * 3600 + int(mins) * 60 + float(secs)


def _to_ass_time(time):
    mins, secs = divmod(time, 60)
    hrs, mins = divmod(mins, 60)
    return '%d:%d:%.02f' % (hrs, mins, secs)
