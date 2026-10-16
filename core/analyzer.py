from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import performance as perf
from .common import (APP_NAME, APP_VERSION, AnalysisError, NO_WINDOW,
                     SUPPORTED_QP, _frac, _r, load_scoring, mean, percentile)
from .hardware import HardwareMixin, Tools
from .processes import ProcessMixin
from .bitstream import BitstreamMixin
from .segments import SegmentsMixin
from .pixel_pipeline import PixelPipelineMixin
from .scoring import ScoringMixin

class Analyzer(ProcessMixin, BitstreamMixin, SegmentsMixin, PixelPipelineMixin, HardwareMixin, ScoringMixin):
    def __init__(
        self,
        tools: Tools,
        scoring: dict | None = None,
        use_cambi: bool = True,
        cambi_subsample: int | None = None,
        progress=None,
        mode="balanced",
        force=False,
        cache=True,
    ):
        if mode not in ("fast", "balanced", "accurate"):
            raise ValueError("Unknown analysis mode")
        self.mode, self.force, self.cache = mode, force, cache
        self.timings = {}
        self.execution = []
        self.max_processes = 0
        self._tuning = False
        self.selected_segments = 1
        self.selected_threads = 8
        self._frame_times = None
        self._frames_ready = threading.Event()
        self._gpu_pending = set()
        self.tools = tools
        self.sc = scoring or load_scoring()
        self.use_cambi = use_cambi
        self.cambi_subsample = cambi_subsample or (1 if mode == "accurate" else None)
        self.progress = progress or (lambda phase, frac, msg: None)
        self._cancel = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._procs: set[subprocess.Popen] = set()
        self._proc_lock = threading.Lock()
        self._prog_lock = threading.Lock()
        self._bit_frac = 0.0
        self._pix_frac = 0.0
        self._pix_active = False
        self._analysis_started = None
        self._progress_info = None
        self._fps_sample = None
        self._fps_smoothed = None
        self.filter_complex_threads = 1
        self.selected_filter_threads = 1
        self.hw_loglevel = "debug"
        self.trust_hw_for_benchmark = False

    def _timed(self, name, fn, *args):
        start = time.perf_counter()
        try:
            return fn(*args)
        finally:
            self.timings[name] = time.perf_counter() - start

    def probe(self, path):
        cmd = [
            self.tools.ffprobe, "-v", "error", "-show_streams",
            "-show_format", "-of", "json", path,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
        self._register_proc(proc)
        try:
            stdout,stderr = proc.communicate(timeout=120)
            r = subprocess.CompletedProcess(cmd,proc.returncode,stdout,stderr)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            self._unregister_proc(proc)
            if proc.stderr:
                proc.stderr.close()
        self._check()
        if r.returncode != 0:
            err_tail = r.stderr.strip()[-500:]
            raise AnalysisError("ffprobe nie może odczytać pliku:\n" + err_tail)
        data = json.loads(r.stdout or "{}")
        vs = [
            s for s in data.get("streams", [])
            if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")
        ]
        if not vs:
            raise AnalysisError("Plik nie zawiera strumienia wideo.")
        s = vs[0]
        fmt = data.get("format", {})
        fps = (
            _frac(s.get("avg_frame_rate", "0/0"))
            or _frac(s.get("r_frame_rate", "0/0"))
            or 30.0
        )
        dur = float(s.get("duration") or fmt.get("duration") or 0) or 0.0
        pix = s.get("pix_fmt", "") or ""
        bitdepth = 12 if "12" in pix else 10 if "10" in pix else 8
        try:
            bitdepth = int(s.get("bits_per_raw_sample") or bitdepth)
        except ValueError:
            pass
        nb = int(s.get("nb_frames") or 0) or int(round(dur * fps)) or 0
        return {
            "storage_root": os.path.splitdrive(os.path.abspath(path))[0],
            "index": s["index"],
            "codec": s.get("codec_name", "?"),
            "codec_long": s.get("codec_long_name", ""),
            "profile": s.get("profile", ""),
            "width": int(s.get("width", 0)),
            "height": int(s.get("height", 0)),
            "pix_fmt": pix,
            "rotation": int(round(float(next((d.get('rotation', 0) for d in s.get('side_data_list', []) if 'rotation' in d), s.get('tags', {}).get('rotate', 0))))) % 360,
            "color_range": s.get('color_range'),
            "color_space": s.get('color_space'),
            "color_transfer": s.get('color_transfer'),
            "color_primaries": s.get('color_primaries'),
            "bitdepth": bitdepth,
            "fps": fps,
            "nominal_fps": _frac(s.get("r_frame_rate", "0/0")),
            "time_base": _frac(s.get("time_base", "1/1000")),
            "duration": dur,
            "nb_frames": nb,
            "stream_bitrate": float(s.get("bit_rate") or 0),
            "container": fmt.get("format_long_name", fmt.get("format_name", "")),
            "file_size": int(fmt.get("size") or os.path.getsize(path)),
        }

    def analyze(self, path):
        def pids():
            with self._proc_lock:
                return [os.getpid()] + [p.pid for p in self._procs if p.poll() is None]
        memory = perf.PeakMemory(pids)
        start = time.perf_counter()
        source_stat = os.stat(path)
        source_identity = (source_stat.st_size, source_stat.st_mtime_ns)
        memory.start()
        self._analysis_active = True
        try:
            result = self._analyze(path)
        finally:
            self._analysis_active = False
            peak = memory.stop()
        if not result.get('cache_hit'):
            result['timings']['total'] = time.perf_counter()-start
            result['stats']['analysis_seconds'] = round(result['timings']['total'], 3)
            result['stats']['analysis_fps'] = len(result['frames']) / max(result['timings']['total'],1e-9)
            result['stats']['peak_working_set_bytes'] = peak
            result['stats']['max_ffmpeg_ffprobe_processes'] = self.max_processes
            result['stats']['memory_measurement'] = '100 ms sampled sum of Python + registered FFmpeg working sets; shared pages may be counted twice'
            self._check()
            if self.cache:
                current_stat = os.stat(path)
                if (current_stat.st_size, current_stat.st_mtime_ns) != source_identity:
                    result['warnings'].append('Plik zmienił się podczas analizy; wynik nie został zapisany w cache.')
                    return result
                key = perf.result_key(path, [self.sc, self.use_cambi, self.cambi_subsample, self.mode], self.tools)
                try:
                    perf.atomic_json(perf.data_dir() / ('result-'+key+'.json'), result)
                except (OSError,ValueError) as e:
                    result['warnings'].append('Nie zapisano cache: '+str(e))
        return result

    def _analyze(self, path):
        if not self.tools.ok:
            raise AnalysisError("Brak działającego ffmpeg/ffprobe (z filtrem trace_headers).")
        path = os.path.abspath(path)
        t_start = time.monotonic()
        self.timings, self.execution = {}, []
        self.max_processes = 0
        self._frame_times = None
        self.selected_segments, self.selected_threads = 1, 8
        self._frames_ready.clear()
        self._bit_frac = self._pix_frac = 0.0
        self._pix_active = False
        self._analysis_started = time.monotonic()
        self._progress_info = None
        self._fps_sample = None
        self._fps_smoothed = None
        key = perf.result_key(path, [self.sc, self.use_cambi, self.cambi_subsample, self.mode], self.tools)
        cache_path = perf.data_dir() / ("result-" + key + ".json")
        self._check()
        if self.cache and not self.force:
            cached = perf.read_json(cache_path)
            if isinstance(cached, dict) and all(k in cached for k in ("frames", "scores", "stats", "info")):
                cached["cache_hit"] = True
                cached["cache_load_seconds"] = time.monotonic() - t_start
                self._check()
                return cached
        warnings = []
        self.progress("probe", 0.0, "Odczyt informacji o pliku…")
        info = self._timed("ffprobe", self.probe, path)
        self._progress_info = info
        self._check()
        if info["codec"] not in SUPPORTED_QP:
            warnings.append(
                f"Kodek {info['codec']} nieobsługiwany przy odczycie QP – "
                "ocena tylko z bitrate i CAMBI."
            )

        if self.mode == "fast":
            frames = self._timed("bitstream_pass", self._headers, path, info)
            siti, cambi, n_sub = self._timed("pixel_pass", self.sampled_pixel_pass, path, info, frames)
            warnings.append("Wynik PRZYBLIŻONY: SI/TI i CAMBI z ciągłych fragmentów; pozostałe okna używają średnich z próbek.")
        else:
            with ThreadPoolExecutor(max_workers=2) as ex:
                fut_bit = ex.submit(self._timed, "bitstream_pass", self._headers, path, info)
                fut_pix = ex.submit(self._timed, "pixel_pass", self.pixel_pass, path, info)
                try:
                    frames = fut_bit.result()
                    siti, cambi, n_sub = fut_pix.result()
                except Exception:
                    self.cancel()
                    raise
        if info["codec"] in SUPPORTED_QP and all(f["qp"] is None for f in frames):
            warnings.append("Nie znaleziono wartości QP w nagłówkach strumienia.")
        if self.use_cambi and not self.tools.has_libvmaf:
            warnings.append(
                "ffmpeg bez libvmaf – CAMBI (banding) pominięte. "
                "Użyj pełnej wersji ffmpeg (np. gyan.dev 'full' lub BtbN 'gpl')."
            )
        if not self.tools.has_siti:
            warnings.append(
                "ffmpeg bez filtra siti – bitrate oceniany bez normalizacji złożoności."
            )
        if self.mode != 'fast' and self.tools.has_siti and len(siti) != len(frames):
            warnings.append(f'Nagłówki zawierają {len(frames)} pakietów, a SI/TI {len(siti)} zdekodowanych obrazów. Pakiet nie zawsze odpowiada wyświetlonej klatce; danych nie uzupełniano sztucznie.')
        self._check()
        self.progress("score", 1.0, "Liczenie oceny…")
        windows, scores = self._timed("scoring", self.score, info, frames, siti, cambi)

        sizes = [f["size"] for f in frames]
        qps_raw = [f["qp_raw"] for f in frames if f["qp_raw"] is not None]
        qps_eq = [f["qp"] for f in frames if f["qp"] is not None]
        dur = info["duration"] or (len(frames) / info["fps"])
        i_idx = [i for i, f in enumerate(frames) if f["type"] == "I"]
        gop = mean([b - a for a, b in zip(i_idx, i_idx[1:])]) if len(i_idx) > 1 else None
        qp_scale = "base_q_idx 0-255" if info["codec"] in ("av1", "vp9") else "QP 0-51"
        stats = {
            "frames": len(frames),
            "count_I": sum(1 for f in frames if f["type"] == "I"),
            "count_P": sum(1 for f in frames if f["type"] == "P"),
            "count_B": sum(1 for f in frames if f["type"] == "B"),
            "gop_avg": _r(gop),
            "bitrate_mbps": _r(sum(sizes) * 8 / dur / 1e6, 2) if dur else None,
            "bpp": _r(sum(sizes) * 8 / (max(1, len(frames) * info["width"] * info["height"])), 4),
            "frame_size_avg": _r(mean(sizes), 0),
            "frame_size_max": max(sizes, default=0),
            "qp_raw_mean": _r(mean(qps_raw)),
            "qp_raw_min": _r(min(qps_raw)) if qps_raw else None,
            "qp_raw_max": _r(max(qps_raw)) if qps_raw else None,
            "qp_eq_mean": _r(mean(qps_eq)),
            "qp_scale": qp_scale,
            "si_mean": _r(mean([s["si"] for s in siti])),
            "ti_mean": _r(mean([s["ti"] for s in siti])),
            "cambi_mean": _r(mean([c["cambi"] for c in cambi]), 2),
            "cambi_max": _r(max([c["cambi"] for c in cambi]), 2) if cambi else None,
            "cambi_p95": _r(percentile([c["cambi"] for c in cambi], 95), 2),
            "cambi_subsample": n_sub,
            "analysis_seconds": round(time.monotonic() - t_start, 1),
        }
        if not frames:
            raise AnalysisError("Brak klatek w analizowanym pliku.")
        self.timings["total"] = time.monotonic() - t_start
        stats["analysis_fps"] = len(frames) / max(self.timings["total"], 1e-9)
        stats["decoder"] = ", ".join(sorted({x["decoder"] for x in self.execution})) or "none"
        result = {
            "cache_hit": False,
            "mode": self.mode,
            "approximate": self.mode == "fast",
            "sample_intervals": getattr(self, "sample_intervals", []),
            "timings": self.timings,
            "execution": self.execution,
            "app": f"{APP_NAME} {APP_VERSION}",
            "file": os.path.abspath(path),
            "name": os.path.basename(path),
            "analyzed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "info": info,
            "stats": stats,
            "scores": scores,
            "warnings": warnings,
            "windows": windows,
            "frames": frames,
            "siti": siti,
            "cambi": cambi,
        }
        self._check()
        self.progress("done", 1.0, "Analiza zakończona")
        return result
