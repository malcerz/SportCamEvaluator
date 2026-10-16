from __future__ import annotations

from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor

from .common import AnalysisError, CPU_COUNT, mean

class SegmentsMixin:
    def sampled_pixel_pass(self, path, info, frames):
        """Sample continuous sequences; QP and packet size guide hard excerpts."""
        self._begin_pixel_progress()
        dur = info['duration'] or len(frames) / info['fps']
        if dur <= 18:
            return self.pixel_pass(path, info)
        self._select_backend(path, info)
        span = 3.0
        buckets = {}
        for f in frames:
            k = int(f['t'] // span)
            b = buckets.setdefault(k, [[], 0, 0])
            if f['qp'] is not None:
                b[0].append(f['qp'])
            b[1] += f['size']
            b[2] += 1
        hard_qp = max(buckets, key=lambda k: mean(buckets[k][0]) or 0)
        hard_bits = max(buckets, key=lambda k: buckets[k][1] / max(buckets[k][2], 1))
        starts = sorted({0.0, max(0.0, dur/2-span/2), max(0.0, dur-span),
                         min(hard_qp*span, dur-span), min(hard_bits*span, dur-span)})
        # Merge overlapping sequences so their weight is not duplicated.
        intervals = []
        for start in starts:
            if intervals and start <= intervals[-1][1]:
                intervals[-1][1] = max(intervals[-1][1], start + span)
            else:
                intervals.append([start, start + span])
        total = sum(b-a for a,b in intervals)
        siti, cambi = [], []
        done = 0.0
        nsub = self.cambi_subsample or max(1, round(info['nb_frames']/400))
        for idx, (start, end) in enumerate(intervals):
            self._check()
            def progress(i, t):
                self._report_parallel_progress('pixels', (done+min(t,end-start))/total,
                                               'Próbkowanie ciągłych fragmentów (wynik przybliżony)')
            kwargs = {}
            seek, length = start, end-start
            if self._frame_times:
                ts = self._frame_times
                a,b = bisect_left(ts,start),bisect_left(ts,end)
                warm = 1 if a else 0
                seek = max(0.0, ts[a-warm]-.000051)
                stop = ts[b]-.000051 if b<len(ts) else None
                length = stop-seek if stop is not None else None
                kwargs = dict(skip_frames=warm, output_start=ts[a],
                              cambi_frame_offset=a, frame_count=b-a+warm)
            s, c = self._run_pixel_segment(path, info, idx, seek, length,
                self.tools.has_siti, self.use_cambi and self.tools.has_libvmaf,
                nsub, min(8, max(1, CPU_COUNT-2)),
                bool(self.tools.hwaccel), progress, **kwargs)
            siti.extend(s)
            cambi.extend(c)
            done += end-start
        self.sample_intervals = intervals
        return siti, cambi, nsub if cambi else None

    def continuous_segments(self, path, info, count, n_sub, threads, duration=None, start_offset=0.0):
        """CFR segments with one preceding frame and a global CAMBI phase.

        Only used after an exact comparison against a continuous decode.
        VFR sources keep a single continuous decode.
        """
        fps = info['fps']
        if self._frame_times:
            start_frame = bisect_left(self._frame_times, start_offset) if start_offset else 0
            end_frame = (bisect_left(self._frame_times, start_offset + duration)
                         if duration else info['nb_frames'])
            total_frames = end_frame - start_frame
        else:
            start_frame = max(0, round(start_offset*fps))
            total_frames = round(duration*fps) if duration else info['nb_frames']-start_frame
        total_frames = min(total_frames, max(0, info['nb_frames']-start_frame))
        end_frame = start_frame + total_frames
        if start_frame:
            boundaries = [start_frame]
            for i in range(1, count):
                boundary = (start_frame + int(total_frames*i/count))//n_sub*n_sub
                if boundary <= boundaries[-1]:
                    boundary = boundaries[-1] + n_sub
                boundaries.append(boundary)
            boundaries.append(end_frame)
        else:
            boundaries = [int(total_frames*i/count)//n_sub*n_sub for i in range(count)] + [total_frames]
        if any(b <= a for a,b in zip(boundaries,boundaries[1:])):
            # Very sparse CAMBI may have no sample inside an entire segment.
            boundaries = [int(total_frames*i/count) for i in range(count)] + [total_frames]
        if any(b <= a for a,b in zip(boundaries, boundaries[1:])):
            raise AnalysisError('Za mało klatek do podziału.')
        with self._proc_lock:
            self._gpu_pending = set(range(count)) if self.tools.hwaccel else set()
        times = [0.0]*count
        def progress(idx, t):
            if self._tuning:
                return
            times[idx] = min(t, (boundaries[idx+1]-boundaries[idx])/fps)
            self._report_parallel_progress('pixels', sum(times)/(total_frames/fps),
                                          f'Analiza pikselowa: {count} procesy')
        def run(idx):
            a,b = boundaries[idx:idx+2]
            warm = 1 if a else 0
            phase = (-a) % n_sub
            do_cambi = self.use_cambi and self.tools.has_libvmaf and a+phase < b
            if not do_cambi and not self.tools.has_siti:
                with self._proc_lock:
                    self._gpu_pending.discard(idx)
                return [], []
            timestamps = self._frame_times
            if timestamps and b <= len(timestamps):
                # Header PTS are rounded to 0.1 ms by the existing parser.
                # Seek just before the preceding picture, and stop just before
                # the following boundary; never synthesize a previous TI frame.
                seek = max(0.0, timestamps[a-warm] - .000051)
                end = timestamps[b] - .000051 if b < len(timestamps) else None
                seg_duration = end-seek if end is not None else None
                output_start = timestamps[a]
            else:
                seek = (a-warm)/fps
                seg_duration = (b-a+warm)/fps if duration or idx<count-1 else None
                output_start = a/fps
            return self._run_pixel_segment(path, info, idx, seek,
                seg_duration,
                self.tools.has_siti, do_cambi,
                n_sub, threads, bool(self.tools.hwaccel), progress, warm,
                output_start, a, b-a+warm, cambi_phase=phase)
        s,c = [],[]
        with ThreadPoolExecutor(max_workers=count) as ex:
            fs = [ex.submit(run,i) for i in range(count)]
            try:
                for f in fs:
                    si,ca = f.result()
                    s.extend(si);c.extend(ca)
            except Exception:
                self.cancel()
                raise
        return sorted(s,key=lambda x:x['t']),sorted(c,key=lambda x:x['t'])
