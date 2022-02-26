import bisect

class NonLinearTime:
    def __init__(self, timeline):
        self.cache = []
        current_output_time = 0
        for (start_time, relative_speed), (end_time, _) in zip(timeline, timeline[1:]):
            self.cache.append((start_time, end_time, current_output_time, relative_speed))
            current_output_time += (end_time - start_time) * relative_speed
        self.cache.append((timeline[-1][0], None, current_output_time, timeline[-1][1]))
        self.times = tuple(x[0] for x in self.cache)

    def convert(self, input_time):
        index = bisect.bisect_left(self.times, input_time)-1
        start_time, end_time, output_time, relative_speed = self.cache[index]
        return output_time + (input_time - start_time) * relative_speed

    def generate_setpts_expr(self):

        # the expression we generate is:
        # if(lt(PTS, <endtime1>/TB),
        #     <outtime1>+(PTS-<starttime1>/TB)*<factor>,
        #     if(lt(PTS, <endtime2>/TB), outtime2+(PTS-starttime2)*<factor2>, ...))

        # ffmpeg will evaluate this expression once for each frame in the video, passing in a different value for
        # PTS each time.

        # PTS is the input (p)resentation (t)ime(s)tamp of the current frame, as an integer.  TB is the timebase --
        # the smallest measurable difference, in seconds, between two frames.
        # Usually it's just the reciprocal of the video's framerate.
        # Were ffmpeg playing the video rather than transocding it, the current frame would be displayed (PTS*TB)
        # seconds into the video.

        expr = '{2:.2f}/TB+(PTS-{0:.2f}/TB)*{3}'.format(*self.cache[-1])
        for start_time, end_time, output_time, relative_speed in reversed(self.cache[:-1]):
            # ffmpeg makes you escape commas, since they are used to delimit the video filter list.




            expr = r'if(lt(PTS\,{1:.2f}/TB)\,{2:.2f}/TB+(PTS-{0:.2f}/TB)*{3}\,{4})'.format(start_time,end_time,
                                                                                          output_time,
                                                                                          relative_speed,expr)

        return expr

    def generate_chunked_setpts_exprs(self, max_depth=100, max_length=32767):
        expr  = '!'
        last_good_expr = None
        # start current depth at 1 because if(lt( adds two levels of nesting, even though we subtract one immediately
        # after when we add the closing parenthesis of the lt
        # 0 -> 1 -> 2 -> 1 -> 2 -> 3 -> 2 -> ...
        # if at any point our current depth gets above ffmpeg's limit, ffmpeg will fail, so we start at 1 to avoid
        # this scenario
        current_depth = 1
        current_expr_start_time = 0
        for start_time, end_time, output_time, relative_speed in self.cache:
            if end_time is None:
                break
            # do expr=new_expr in the top of the loop rather than in the else clause below (where it would make more
            # sense) because we need to reference expr after the loop
            new_expr = expr.replace('!', r'if(lt(PTS\,{1:.2f}/TB)\,{2:.2f}/TB+(PTS-{0:.2f}/TB)*{3}\,!)'
                                    .format(start_time-current_expr_start_time,
                                            end_time-current_expr_start_time,
                                            output_time, relative_speed))
            terminal_expr = expr.replace('!', '{1:.2f}/TB+(PTS-{0:.2f}/TB)*{2}'
                                         .format(start_time-current_expr_start_time, output_time, relative_speed))
            current_depth += 1
            if len(terminal_expr) < max_length and current_depth < max_depth:
                last_good_expr = terminal_expr
            if len(terminal_expr) > max_length or current_depth >= max_depth:
                yield current_expr_start_time, end_time, last_good_expr
                current_expr_start_time = start_time
                current_depth = 0
                expr = r'if(lt(PTS\,{1:.2f}/TB)\,{2:.2f}/TB+(PTS-{0:.2f}/TB)*{3}\,!)'.format(
                    start_time-current_expr_start_time, end_time-current_expr_start_time, output_time, relative_speed)
            else:
                expr = new_expr
        yield current_expr_start_time, end_time, last_good_expr


