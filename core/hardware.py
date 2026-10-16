from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time

import performance as perf
from .common import AnalysisError, CPU_COUNT, IS_WIN, NO_WINDOW, _run, find_tool

class Tools:
    """Wykryte ffmpeg/ffprobe i ich możliwości."""

    def __init__(self, configured_dir: str | None = None):
        self.ffmpeg = find_tool("ffmpeg", configured_dir)
        self.ffprobe = find_tool("ffprobe", configured_dir)
        self.version = ""
        self.has_libvmaf = False
        self.has_siti = False
        self.has_trace = False
        self.hwaccels = []
        self.cambi_only = False
        self.hwaccel: str | None = None
        if self.ffmpeg:
            try:
                v = _run([self.ffmpeg, "-hide_banner", "-version"]).stdout
                self.version = v.splitlines()[0] if v else ""
                flt = _run([self.ffmpeg, "-hide_banner", "-filters"]).stdout
                self.has_libvmaf = re.search(r"\slibvmaf\s", flt) is not None
                self.has_siti = re.search(r"\ssiti\s", flt) is not None
                bsf = _run([self.ffmpeg, "-hide_banner", "-bsfs"]).stdout
                self.has_trace = "trace_headers" in bsf
                hw = _run([self.ffmpeg, "-hide_banner", "-hwaccels"]).stdout
                self.hwaccels = [x for x in ("cuda", "d3d11va", "dxva2", "qsv", "vaapi", "videotoolbox") if x in hw.split()]
                if IS_WIN:
                    if "d3d11va" in hw:
                        self.hwaccel = "d3d11va"
                    elif "dxva2" in hw:
                        self.hwaccel = "dxva2"
                else:
                    for cand in ("vaapi", "videotoolbox", "cuda"):
                        if cand in hw:
                            self.hwaccel = cand
                            break
            except Exception:
                pass

    @property
    def ok(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe and self.has_trace)

    def describe(self) -> str:
        if not self.ffmpeg or not self.ffprobe:
            return "Nie znaleziono ffmpeg/ffprobe"
        parts = [self.version.replace("ffmpeg version ", "ffmpeg ")[:36]]
        parts.append("GPU dostępne: " + "/".join(self.hwaccels[:2]) if self.hwaccels else "GPU: brak")
        parts.append(f"CPU: {CPU_COUNT}W")
        parts.append("CAMBI: tak" if self.has_libvmaf else "CAMBI: brak libvmaf")
        if not self.has_siti:
            parts.append("brak filtra siti")
        return " | ".join(parts)

class HardwareMixin:
    def _verify_cambi_only(self):
        try:
            self._probe_cambi_only()
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            self.tools.cambi_only = False
        self._check()

    def _probe_cambi_only(self):
        """Verify extractor equality AND absence of model metrics on 8/10 bit."""
        key = perf.digest([self.tools.version, os.stat(self.tools.ffmpeg).st_mtime_ns])
        cp = perf.data_dir() / ('capability-' + key + '.json')
        saved = perf.read_json(cp)
        if saved is not None:
            self.tools.cambi_only = saved.get('cambi_only', False)
            return
        valid = True
        with tempfile.TemporaryDirectory(prefix='scev_cap_') as td:
            for fmt in ('yuv420p', 'yuv420p10le'):
                values = []
                for model in ('version=vmaf_v0.6.1', '|'):
                    self._check()
                    cmd = [self.tools.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                           'testsrc2=size=320x240:rate=5:duration=0.6',
                           '-filter_complex_threads', '1', '-filter_complex',
                           f'format={fmt},split[a][b];[a][b]libvmaf=model={model}:'
                           'feature=name=cambi:log_fmt=json:log_path=result.json[o]',
                           '-map', '[o]', '-f', 'null', '-']
                    proc = subprocess.Popen(cmd, cwd=td, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE, creationflags=NO_WINDOW)
                    self._register_proc(proc)
                    try:
                        _, err = proc.communicate(timeout=30)
                    finally:
                        if proc.poll() is None:
                            proc.kill()
                            proc.wait()
                        self._unregister_proc(proc)
                    if proc.returncode:
                        valid = False
                        break
                    data = perf.read_json(os.path.join(td, 'result.json'))
                    fs = data.get('frames', []) if data else []
                    values.append([f['metrics'].get('cambi') for f in fs])
                    if not fs or (model == '|' and any(set(f['metrics']) != {'cambi'} for f in fs)):
                        valid = False
                valid = valid and len(values) == 2 and values[0] == values[1]
        self._check()
        self.tools.cambi_only = bool(valid)
        try:
            perf.atomic_json(cp, {'cambi_only': bool(valid)})
        except OSError:
            pass

    def _backend_key(self, info):
        gpu = ''
        try:
            gpu = _run(['nvidia-smi', '--query-gpu=name,uuid,driver_version',
                        '--format=csv,noheader'], timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._gpu_identity = gpu
        return perf.digest([perf.machine_key(), gpu, self.tools.version,
                            os.stat(self.tools.ffmpeg).st_mtime_ns,
                            [info[k] for k in ('codec', 'width', 'height', 'bitdepth', 'pix_fmt')],
                            [info.get(k) for k in ('rotation','color_range','color_space','color_transfer','color_primaries','profile')],
                            self.use_cambi, self.cambi_subsample or max(1,round(info['nb_frames']/400)), self.mode,
                            info.get('storage_root'), self.sc.get('threads_per_process'),
                            self.sc.get('parallel_segments'), self.sc.get('filter_complex_threads'),
                            perf.ENGINE_REVISION])

    def _select_backend(self, path, info):
        if self.use_cambi and self.tools.has_libvmaf:
            self._verify_cambi_only()
        if not self.sc.get('use_hwaccel', True):
            self.tools.hwaccel = None
            return
        cp = perf.data_dir() / ('backend-' + self._backend_key(info) + '.json')
        saved = perf.read_json(cp)
        if saved:
            self.tools.hwaccel = saved.get('backend')
            self.selected_segments = saved.get('segments', 1)
            self.selected_threads = saved.get('threads', 8)
            self.selected_filter_threads = saved.get('filter_threads', 1)
            self.hw_loglevel = saved.get('loglevel', 'error')
            self.trust_hw_for_benchmark = bool(
                saved.get('trust_hw_after_calibration', self.tools.hwaccel is not None))
            self.timings['backend_cached'] = True
            return
        # A short source cannot amortize a multi-decoder benchmark.
        if info['duration'] < 90:
            self.tools.hwaccel = None
            return
        start = time.perf_counter()
        self.progress('decoder', 0, 'Krótki benchmark CPU/GPU z filtrami…')
        report = self.benchmark(path, info=info, seconds=1.5 if self.mode == 'fast' else 6.0,
                                full=self.mode != 'fast', automatic=True)
        self.timings['backend_benchmark'] = time.perf_counter() - start
        winner = report['winner']
        self.tools.hwaccel = None if winner == 'cpu' else winner
        self.selected_segments = report.get('recommended_segments', 1)
        self.selected_threads = report.get('recommended_threads', 8)
        self.selected_filter_threads = report.get('recommended_filter_threads', 1)
        self.hw_loglevel = report.get('recommended_loglevel', 'error')
        self.trust_hw_for_benchmark = bool(report.get('trust_hw_after_calibration', False))
        try:
            perf.atomic_json(cp, {'backend': self.tools.hwaccel, 'segments': self.selected_segments,
                                 'threads': self.selected_threads,
                                 'filter_threads': self.selected_filter_threads,
                                 'loglevel': self.hw_loglevel,
                                 'trust_hw_after_calibration': self.trust_hw_for_benchmark,
                                 'benchmark': report})
        except OSError:
            pass

    def benchmark(self, path, info=None, seconds=6.0, full=True, automatic=False,
                  repeats=3, process_counts=(1, 2, 3, 4, 6), filter_counts=(1, 2, 4)):
        """Measure repeatable decoded-frame pipelines without writing video files."""
        from statistics import median

        info = info or self.probe(path)
        self._verify_cambi_only()
        self._backend_key(info)
        if full and (not self._frames_ready.is_set() or not self._frame_times):
            if getattr(self, '_analysis_active', False):
                while not self._frames_ready.wait(.1):
                    self._check()
            else:
                self._headers(path, info)
        saved = (self.tools.hwaccel, self.execution, self.filter_complex_threads,
                 self.selected_filter_threads, self.hw_loglevel,
                 self.trust_hw_for_benchmark)
        self.execution = []
        self._tuning = True
        nsub = self.cambi_subsample or max(1, round(info['nb_frames'] / 400))
        repeats = max(1, int(repeats))
        offsets = [0.0]
        backend_rows, diagnostics, references = [], [], {}
        estimate = max(512 * 1024**2,
                       info['width'] * info['height'] * (2 if info['bitdepth'] > 8 else 1) * 96)
        available = perf.available_memory()
        try:
            all_backends = [None] + list(self.tools.hwaccels)
            if automatic:
                order = (('cuda', 'd3d11va', 'videotoolbox', 'vaapi', 'qsv')
                         if getattr(self, '_gpu_identity', '')
                         else ('d3d11va', 'videotoolbox', 'vaapi', 'qsv', 'cuda'))
                available_hw = [x for x in order if x in self.tools.hwaccels]
                # On Windows NVIDIA, compare NVDEC/CUDA with the D3D11 path.
                # Other adapters try their best available path and CPU fallback.
                backends = [None] + available_hw[:2 if self.mode != 'fast' else 1]
            else:
                backends = all_backends

            # Establish a CPU reference and verify each requested decoder three times.
            for backend in backends:
                self.tools.hwaccel = backend
                elapsed_rows, equal_rows, actual_rows, errors = [], [], [], []
                result_sets = []
                for rep in range(repeats):
                    self._check()
                    start = time.perf_counter()
                    try:
                        s, c = self._run_pixel_segment(
                            path, info, 0, 0.0,
                            min(seconds, info['duration']) if info['duration'] else seconds,
                            self.tools.has_siti, self.use_cambi and self.tools.has_libvmaf,
                            nsub, max(1, (CPU_COUNT - 2 - 1) // 2), backend is not None,
                            lambda *a: None, allow_fallback=False)
                        elapsed_rows.append(time.perf_counter() - start)
                        actual = self.execution[-1]['decoder']
                        actual_rows.append(actual)
                        result_sets.append((s, c))
                    except AnalysisError as e:
                        errors.append(str(e)[-500:])
                if result_sets and backend is None:
                    references[0.0] = result_sets[0]
                reference = references.get(0.0)
                equal_rows = [r == reference for r in result_sets]
                valid = bool(result_sets) and all(equal_rows) and all(
                    x == (backend or 'cpu') for x in actual_rows)
                row = {
                    'backend': backend or 'cpu', 'actual': actual_rows[-1] if actual_rows else None,
                    'seconds': elapsed_rows, 'median_seconds': median(elapsed_rows) if elapsed_rows else None,
                    'aggregate_source_fps': (seconds * info['fps'] / median(elapsed_rows)
                                             if elapsed_rows else None),
                    'repeats': len(elapsed_rows), 'equivalent': bool(equal_rows) and all(equal_rows),
                    'valid': valid,
                }
                if errors:
                    row['errors'] = errors
                backend_rows.append(row)

            valid_backends = [r for r in backend_rows if r['valid']]
            if automatic:
                candidate_backends = [min(valid_backends, key=lambda r: r['median_seconds'])['backend']] if valid_backends else ['cpu']
            else:
                candidate_backends = [r['backend'] for r in valid_backends]

            parallel_references = {}
            for backend in candidate_backends:
                self.tools.hwaccel = None if backend == 'cpu' else backend
                self.filter_complex_threads = 1
                # Use one fixed short range for throughput comparisons and a
                # single longer run per topology for steady-state RAM. This
                # keeps calibration useful without repeating long runs three
                # times for every process count.
                memory_seconds = max(seconds, max(process_counts, default=1) * 4.0)
                ref_threads = max(1, (CPU_COUNT - 2) // 2)

                def make_reference(sample_length):
                    from bisect import bisect_left
                    timestamps = self._frame_times
                    start_frame = bisect_left(timestamps, offsets[0])
                    end_frame = bisect_left(timestamps, offsets[0] + sample_length)
                    warm = 1 if start_frame else 0
                    seek = max(0.0, timestamps[start_frame-warm] - .000051)
                    stop = timestamps[end_frame] - .000051 if end_frame < len(timestamps) else None
                    ref_duration = stop-seek if stop is not None else None
                    values = self._run_pixel_segment(
                        path, info, 0, seek, ref_duration,
                        self.tools.has_siti, self.use_cambi and self.tools.has_libvmaf,
                        nsub, ref_threads, backend != 'cpu', lambda *a: None,
                        skip_frames=warm, output_start=timestamps[start_frame],
                        cambi_frame_offset=start_frame,
                        frame_count=end_frame-start_frame+warm,
                        allow_fallback=False)
                    return values, self.execution[-1]['decoder']

                timing_reference, timing_decoder = make_reference(seconds)
                memory_reference, memory_decoder = make_reference(memory_seconds)
                parallel_references[backend] = (timing_reference, timing_decoder,
                                                memory_reference, memory_decoder)
                for count in process_counts:
                    count = int(count)
                    if count < 1:
                        continue
                    # Reserve at least 55% of currently available RAM for Windows and other apps.
                    available = perf.available_memory()
                    if available and count * estimate > available * 0.45:
                        diagnostics.append({'backend': backend, 'processes': count,
                                            'filter_threads': 1, 'valid': False,
                                            'skipped': 'conservative RAM budget'})
                        continue
                    threads = max(1, (CPU_COUNT - 2 - count) // (2 * count))
                    elapsed_rows, parity_rows, decoder_rows = [], [], []
                    for rep in range(repeats):
                        self._check()
                        before = len(self.execution)
                        start = time.perf_counter()
                        values = self.continuous_segments(
                            path, info, count, nsub, threads, seconds, start_offset=offsets[0])
                        elapsed_rows.append(time.perf_counter() - start)
                        decoders = {x['decoder'] for x in self.execution[before:]}
                        decoder_rows.append(decoders)
                        parity_rows.append(bool(values == timing_reference
                                                and decoders == {backend}
                                                and timing_decoder == backend))

                    # One warmed, four-second-per-worker run captures buffer
                    # and frame-transfer working sets for this process count.
                    def process_ids():
                        with self._proc_lock:
                            return [os.getpid()] + [p.pid for p in self._procs if p.poll() is None]
                    memory = perf.PeakMemory(process_ids)
                    memory.start()
                    before = len(self.execution)
                    try:
                        memory_values = self.continuous_segments(
                            path, info, count, nsub, threads, memory_seconds,
                            start_offset=offsets[0])
                    finally:
                        peak_memory = memory.stop()
                    memory_decoders = {x['decoder'] for x in self.execution[before:]}
                    memory_equivalent = bool(memory_values == memory_reference
                                             and memory_decoders == {backend}
                                             and memory_decoder == backend)
                    diagnostics.append({
                        'backend': backend, 'processes': count, 'threads_each': threads,
                        'filter_threads': 1, 'seconds': elapsed_rows,
                        'median_seconds': median(elapsed_rows),
                        'sample_seconds': seconds,
                        'aggregate_source_fps': seconds * info['fps'] / median(elapsed_rows),
                        'memory_sample_seconds': memory_seconds,
                        'memory_sample_repeats': 1,
                        'peak_working_set_bytes': peak_memory,
                        'equivalent': all(parity_rows) and memory_equivalent,
                        'valid': all(parity_rows) and memory_equivalent,
                        'repeats': len(elapsed_rows),
                    })

            # Once the fastest parallelism is measured, compare graph scheduling
            # with the same decoder and segment count. Keep the default if no exact
            # output match exists.
            parallel = [r for r in diagnostics if r.get('valid') and r.get('processes', 1) > 1]
            fastest_parallel = min(parallel, key=lambda r: r['median_seconds']) if parallel else None
            if fastest_parallel and full:
                backend = fastest_parallel['backend']
                count = fastest_parallel['processes']
                threads = fastest_parallel['threads_each']
                self.tools.hwaccel = None if backend == 'cpu' else backend
                parallel_reference, reference_decoder, _, _ = parallel_references[backend]
                sample_seconds = seconds
                for filter_threads in filter_counts:
                    filter_threads = int(filter_threads)
                    if filter_threads <= 1:
                        continue
                    self.filter_complex_threads = filter_threads
                    elapsed_rows, parity_rows, memory_rows = [], [], []
                    for rep in range(repeats):
                        self._check()
                        def process_ids():
                            with self._proc_lock:
                                return [os.getpid()] + [p.pid for p in self._procs if p.poll() is None]
                        memory = perf.PeakMemory(process_ids)
                        memory.start()
                        before = len(self.execution)
                        start = time.perf_counter()
                        try:
                            values = self.continuous_segments(
                                path, info, count, nsub, threads, sample_seconds, start_offset=offsets[0])
                            elapsed_rows.append(time.perf_counter() - start)
                        finally:
                            memory_rows.append(memory.stop())
                        decoders = {x['decoder'] for x in self.execution[before:]}
                        parity_rows.append(bool(values == parallel_reference and decoders == {backend}
                                                and reference_decoder == backend))
                    diagnostics.append({
                        'backend': backend, 'processes': count, 'threads_each': threads,
                        'filter_threads': filter_threads, 'seconds': elapsed_rows,
                        'median_seconds': median(elapsed_rows),
                        'sample_seconds': sample_seconds,
                        'aggregate_source_fps': sample_seconds * info['fps'] / median(elapsed_rows),
                        'peak_working_set_bytes': max((m for m in memory_rows if m is not None), default=None),
                        'equivalent': all(parity_rows), 'valid': all(parity_rows),
                        'repeats': len(elapsed_rows),
                    })

            if full and candidate_backends:
                backend = candidate_backends[0] if automatic else (fastest_parallel or {}).get('backend', candidate_backends[0])
                self.tools.hwaccel = None if backend == 'cpu' else backend
                self.filter_complex_threads = 1
                for name, si, ca in (('siti', True, False), ('cambi', False, True)):
                    if (si and not self.tools.has_siti) or (ca and not self.tools.has_libvmaf):
                        continue
                    times = []
                    for _ in range(repeats):
                        start = time.perf_counter()
                        self._run_pixel_segment(path, info, 0, 0.0, seconds, si, ca, nsub,
                                                max(1, (CPU_COUNT-2)//2), self.tools.hwaccel is not None,
                                                lambda *a: None, allow_fallback=False)
                        times.append(time.perf_counter()-start)
                    diagnostics.append({'metric': name, 'backend': backend, 'seconds': times,
                                        'median_seconds': median(times), 'repeats': len(times)})

            candidates = [r for r in diagnostics if r.get('valid') and r.get('processes')]
            if not candidates:
                candidates = [r for r in backend_rows if r['valid']]
            if candidates:
                best = min(candidates, key=lambda r: r['median_seconds'])
                winner = best['backend']
                recommended_segments = best.get('processes', 1)
                recommended_threads = best.get('threads_each', max(1, (CPU_COUNT-2)//2))
                recommended_filter_threads = best.get('filter_threads', 1)
            else:
                winner, recommended_segments = 'cpu', 1
                recommended_threads, recommended_filter_threads = max(1, (CPU_COUNT-2)//2), 1
            report = {
                'file': path, 'winner': winner, 'backends': backend_rows,
                'diagnostics': diagnostics, 'cambi_only': self.tools.cambi_only,
                'recommended_segments': recommended_segments,
                'recommended_threads': recommended_threads,
                'recommended_filter_threads': recommended_filter_threads,
                'seconds_sampled': seconds, 'repeats_requested': repeats,
                'median_based': True,
                'recommended_loglevel': 'error',
                'trust_hw_after_calibration': winner != 'cpu',
                'memory_measurement': '100 ms sampled sum of Python + FFmpeg working sets; shared pages may be counted more than once',
            }
            if full:
                cp = perf.data_dir() / ('backend-' + self._backend_key(info) + '.json')
                try:
                    perf.atomic_json(cp, {
                        'backend': None if winner == 'cpu' else winner,
                        'segments': recommended_segments,
                        'threads': recommended_threads,
                        'filter_threads': recommended_filter_threads,
                        'loglevel': 'error',
                        'trust_hw_after_calibration': winner != 'cpu',
                        'benchmark': report,
                    })
                except OSError:
                    pass
            return report
        finally:
            (self.tools.hwaccel, self.execution, self.filter_complex_threads,
             self.selected_filter_threads, self.hw_loglevel,
             self.trust_hw_for_benchmark) = saved
            self._tuning = False
