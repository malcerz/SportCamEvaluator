"""
SportCamEvaluator - silnik analizy (GPU DirectX + Multi-Segment CPU).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from bisect import bisect_left
from collections import deque
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import performance as perf

APP_NAME = "SportCamEvaluator"
APP_VERSION = "1.2.0"

IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0
CPU_COUNT = max(2, os.cpu_count() or 4)

DEFAULT_SCORING = {
    "qp_curve": [
        [0, 100], [20, 100], [26, 95], [30, 90], [34, 83],
        [38, 73], [42, 60], [46, 42], [51, 15],
    ],
    "qindex_to_qp_a": 14.0,
    "qindex_to_qp_b": 0.116,
    "h264_qp_offset": 3.0,
    "codec_efficiency": {"h264": 1.0, "hevc": 1.5, "vp9": 1.4, "av1": 1.9},
    "bits_req_base": 0.10,
    "bits_si_ref": 60.0,
    "bits_si_exp": 0.7,
    "bits_ti_ref": 20.0,
    "bits_ti_offset": 0.5,
    "bits_k": 1.6,
    "cambi_curve": [
        [0, 100], [1, 100], [3, 85], [5, 65], [8, 40], [12, 15], [20, 0],
    ],
    "weights": {"qp": 0.70, "bits": 0.30},
    "cap_margin": 25.0,
    "cambi_penalty": 0.6,
    "cambi_cap_margin": 20.0,
    "mean_weight": 0.75,
    "window_seconds": 1.0,
    "parallel_segments": 0,
    "use_hwaccel": True,
}

GRADES = [
    (90, "Doskonała"),
    (75, "Bardzo dobra"),
    (60, "Dobra"),
    (40, "Przeciętna"),
    (20, "Słaba"),
    (0, "Bardzo słaba"),
]


def grade_for(score: float) -> str:
    for thr, name in GRADES:
        if score >= thr:
            return name
    return GRADES[-1][1]


class AnalysisError(Exception):
    pass


class Cancelled(Exception):
    pass


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_scoring() -> dict:
    sc = json.loads(json.dumps(DEFAULT_SCORING))
    p = os.path.join(app_dir(), "scoring.json")
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(sc.get(k), dict):
                    sc[k].update(v)
                else:
                    sc[k] = v
        except Exception:
            pass
    return sc


def find_tool(name: str, configured_dir: str | None = None) -> str | None:
    exe = name + (".exe" if IS_WIN else "")
    cands = []
    if configured_dir:
        cands.append(os.path.join(configured_dir, exe))
        cands.append(os.path.join(configured_dir, "bin", exe))
    ad = app_dir()
    cands.append(os.path.join(ad, exe))
    cands.append(os.path.join(ad, "ffmpeg", exe))
    cands.append(os.path.join(ad, "ffmpeg", "bin", exe))
    ffd = os.path.join(ad, "ffmpeg")
    if os.path.isdir(ffd):
        for sub in sorted(os.listdir(ffd)):
            cands.append(os.path.join(ffd, sub, "bin", exe))
    for c in cands:
        if os.path.isfile(c):
            return c
    return shutil.which(name)


def _run(cmd, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=NO_WINDOW,
    )


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


def interp(curve, x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    if x <= curve[0][0]:
        return float(curve[0][1])
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x <= x1:
            t = (x - x0) / (x1 - x0) if x1 != x0 else 0
            return float(y0 + t * (y1 - y0))
    return float(curve[-1][1])


def mean(v):
    v = [x for x in v if x is not None]
    return sum(v) / len(v) if v else None


def percentile(v, p):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * p / 100.0
    f = math.floor(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def _frac(s):
    try:
        if "/" in s:
            a, b = s.split("/")
            return float(a) / float(b) if float(b) else 0.0
        return float(s)
    except Exception:
        return 0.0


RE_PACKET = re.compile(
    r"Packet: (\d+) bytes(, key frame)?.*?, pts (-?\d+|NOPTS|N/A)"
)
RE_FIELD = re.compile(
    r"\]\s+\d+\s+([A-Za-z_][\w\[\]]*)\s+\S*\s*=\s*(-?\d+)\s*$"
)

SUPPORTED_QP = {"h264", "hevc", "av1", "vp9"}


class Analyzer:
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

    def _register_proc(self, p: subprocess.Popen):
        with self._proc_lock:
            self._proc = p
            self._procs.add(p)
            self.max_processes = max(self.max_processes, len(self._procs))

    def _unregister_proc(self, p: subprocess.Popen):
        with self._proc_lock:
            self._procs.discard(p)
            if self._proc is p:
                self._proc = next(iter(self._procs), None)

    def cancel(self):
        self._cancel.set()
        with self._proc_lock:
            procs = list(self._procs)
        for p in procs:
            if p and p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass

    def _check(self):
        if self._cancel.is_set():
            raise Cancelled()

    def _report_parallel_progress(self, source: str, frac: float, msg: str):
        with self._prog_lock:
            if source == "bitstream":
                self._bit_frac = max(self._bit_frac, min(frac, 1.0))
            else:
                self._pix_frac = max(self._pix_frac, min(frac, 1.0))
            total = 0.35 * self._bit_frac + 0.65 * self._pix_frac
            self.progress("analysis", min(total * 0.98, 0.98), msg)

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

    def bitstream_pass(self, path, info):
        codec = info["codec"]
        cmd = [
            self.tools.ffmpeg, "-hide_banner", "-nostats", "-threads", "2",
            "-i", path, "-map", f"0:{info['index']}", "-c", "copy",
            "-bsf:v", "trace_headers", "-f", "null", "-",
        ]
        frames = []
        cur = None
        h_init = {}
        last_pps = 0
        nb = max(info["nb_frames"], 1)
        tb = info["time_base"] or 1 / 1000.0
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW,
            bufsize=1 << 18,
        )
        self._register_proc(proc)
        tail = []
        last_ui = 0.0
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                if self._cancel.is_set():
                    raise Cancelled()
                if "trace_headers" not in line:
                    tail.append(line)
                    if len(tail) > 30:
                        tail.pop(0)
                    continue
                if "Packet:" in line:
                    m = RE_PACKET.search(line)
                    if m:
                        pts = m.group(3)
                        pts_val = int(pts) * tb if pts.lstrip("-").isdigit() else None
                        cur = {
                            "size": int(m.group(1)),
                            "key": bool(m.group(2)),
                            "pts": pts_val,
                            "qps": [],
                            "types": [],
                            "shown": [],
                        }
                        frames.append(cur)
                        now = time.monotonic()
                        if now - last_ui > 0.2:
                            last_ui = now
                            self._report_parallel_progress(
                                "bitstream",
                                min(len(frames) / nb, 1.0),
                                f"Odczyt nagłówków: ramka {len(frames)}/{nb}",
                            )
                    continue
                if "=" not in line:
                    continue
                if not (
                    "qp" in line
                    or "q_idx" in line
                    or "type" in line
                    or "pic_parameter_set_id" in line
                    or "show_frame" in line
                ):
                    continue
                m = RE_FIELD.search(line)
                if not m:
                    continue
                name, val = m.group(1), int(m.group(2))
                if codec in ("hevc", "h264"):
                    if name in (
                        "pps_pic_parameter_set_id",
                        "slice_pic_parameter_set_id",
                        "pic_parameter_set_id",
                    ):
                        last_pps = val
                    elif name in ("init_qp_minus26", "pic_init_qp_minus26"):
                        h_init[last_pps] = 26 + val
                    elif name == "slice_qp_delta" and cur is not None:
                        cur["qps"].append(h_init.get(last_pps, 26) + val)
                    elif name == "slice_type" and cur is not None:
                        if codec == "hevc":
                            t_map = {0: "B", 1: "P", 2: "I"}
                            cur["types"].append(t_map.get(val, "?"))
                        else:
                            t_map = {0: "P", 1: "B", 2: "I", 3: "P", 4: "I"}
                            cur["types"].append(t_map.get(val % 5, "?"))
                else:
                    if cur is None:
                        continue
                    if name == "frame_type":
                        if codec == "av1":
                            cur["types"].append("I" if val in (0, 2) else "P")
                        else:
                            cur["types"].append("I" if val == 0 else "P")
                    elif name == "show_frame":
                        cur["shown"].append(val)
                    elif name == "base_q_idx":
                        cur["qps"].append(val)
            proc.wait()
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass
            self._unregister_proc(proc)
            if proc.stderr:
                proc.stderr.close()
        self._check()
        if proc.returncode != 0:
            raise AnalysisError('Błąd odczytu nagłówków: ' + ''.join(tail[-8:]))
        if not frames:
            err_msg = "Nie udało się odczytać pakietów wideo.\n" + "".join(tail[-8:])
            raise AnalysisError(err_msg)

        out = []
        qa, qb = self.sc["qindex_to_qp_a"], self.sc["qindex_to_qp_b"]
        h264_off = self.sc["h264_qp_offset"]
        for i, f in enumerate(frames):
            qp_raw = mean(f["qps"])
            if qp_raw is None:
                qp_eq = None
            elif codec in ("av1", "vp9"):
                qp_eq = qa + qb * qp_raw
            elif codec == "h264":
                qp_eq = qp_raw + h264_off
            else:
                qp_eq = qp_raw
            if f["types"]:
                if "I" in f["types"]:
                    t = "I"
                elif all(x == "B" for x in f["types"]):
                    t = "B"
                else:
                    t = "P"
            else:
                t = "I" if f["key"] else "P"
            out.append({
                "pts": f["pts"] if f["pts"] is not None else i / info["fps"],
                "size": f["size"],
                "type": t,
                "qp_raw": qp_raw,
                "qp": qp_eq,
            })
        out.sort(key=lambda x: x["pts"])
        t0 = out[0]["pts"]
        for f in out:
            f["t"] = round(f["pts"] - t0, 4)
            del f["pts"]
        return out

    def _headers(self, path, info):
        try:
            frames = self.bitstream_pass(path, info)
            times = [f['t'] for f in frames]
            if len(times) == info['nb_frames'] and all(b>a for a,b in zip(times,times[1:])):
                self._frame_times = times
            return frames
        finally:
            self._frames_ready.set()

    def _run_pixel_segment(
        self,
        path,
        info,
        seg_idx: int,
        seg_start: float,
        seg_dur: float | None,
        do_siti: bool,
        do_cambi: bool,
        n_sub: int,
        threads_per_seg: int,
        use_hw: bool,
        seg_progress_cb,
        skip_frames: int = 0,
        output_start: float | None = None,
        cambi_frame_offset: int | None = None,
        frame_count: int | None = None,
        allow_fallback: bool = True,
        cambi_phase: int = 0,
    ) -> tuple[list, list]:
        cambi_only = self.tools.cambi_only
        cambi_skip = skip_frames + cambi_phase
        src = f"[0:{info['index']}]"
        if frame_count is not None:
            src += f"trim=end_frame={frame_count},"
        pix_target = "yuv420p10le" if info["bitdepth"] >= 10 else "yuv420p"
        siti_chain = (
            "scale=w='min(1280,iw)':h=-2:flags=fast_bilinear,"
            "format=yuv420p,siti,"
            + (f"trim=start_frame={skip_frames}," if skip_frames else "")
            + "metadata=mode=print:file=siti.txt"
        )
        vmaf_opts = (
            f"libvmaf=model={'|' if cambi_only else 'version=vmaf_v0.6.1'}:feature=name=cambi:"
            f"log_fmt=json:log_path=cambi.json:n_subsample={n_sub}:"
            f"n_threads={threads_per_seg}[o]"
        )
        if do_cambi and do_siti:
            fg = (
                f"{src}format={pix_target},split=3[s][a][b];"
                f"[s]{siti_chain},nullsink;"
                + (f"[a]trim=start_frame={cambi_skip}[aa];[b]trim=start_frame={cambi_skip}[bb];[aa][bb]" if cambi_skip else "[a][b]")
                + vmaf_opts
            )
        elif do_cambi:
            fg = f"{src}format={pix_target}," + (f"trim=start_frame={cambi_skip}," if cambi_skip else "") + f"split=2[a][b];[a][b]{vmaf_opts}"
        else:
            fg = f"{src}{siti_chain}[o]"

        tmp = tempfile.mkdtemp(prefix=f"scev_s{seg_idx}_")
        err_path = os.path.join(tmp, "ffmpeg_err.txt")

        def build_cmd(hw_enabled: bool) -> list[str]:
            cmd = [self.tools.ffmpeg, "-hide_banner", "-nostats", "-loglevel", "debug" if hw_enabled else "error",
                   "-filter_complex_threads", "1"]
            if hw_enabled and self.tools.hwaccel:
                cmd += ["-hwaccel", self.tools.hwaccel]
            cmd += ["-threads", str(threads_per_seg)]
            if seg_start > 0:
                cmd += ["-ss", f"{seg_start:.9f}"]
            if seg_dur is not None:
                guard = 2 / info['fps'] if frame_count is not None else 0
                cmd += ["-t", f"{seg_dur + guard:.9f}"]
            cmd += [
                "-i", path,
                "-filter_complex", fg,
                "-map", "[o]",
                "-f", "null", "-",
                "-progress", "pipe:1",
            ]
            return cmd

        try:
            hw_modes = ([True, False] if allow_fallback else [True]) if (use_hw and self.tools.hwaccel) else [False]
            for attempt_hw in hw_modes:
                self._check()
                for fn in ("siti.txt", "cambi.json"):
                    fp = os.path.join(tmp, fn)
                    if os.path.exists(fp):
                        try:
                            os.remove(fp)
                        except OSError:
                            pass

                if not attempt_hw:
                    # Wait for successful GPU siblings before retrying failed
                    # work on CPU; never compete with an untested mixed path.
                    while True:
                        with self._proc_lock:
                            active_gpu = bool(self._gpu_pending) or any(getattr(p, '_scev_gpu', False) and p.poll() is None for p in self._procs)
                        if not active_gpu:
                            break
                        self._cancel.wait(.05)
                        self._check()
                cmd = build_cmd(attempt_hw)
                with open(err_path, "w", encoding="utf-8", errors="replace") as ferr:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=tmp,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=NO_WINDOW,
                    )
                    observed_formats = deque(maxlen=16)
                    def collect_stderr():
                        tail = deque(maxlen=80)
                        selected = deque(maxlen=24)
                        for line in proc.stderr:
                            tail.append(line)
                            if 'chosen by get_format' in line:
                                m = re.search(r'Format (\S+) chosen by get_format', line)
                                if m:
                                    observed_formats.append(m.group(1))
                                selected.append(line)
                            elif any(x in line.lower() for x in ('nvdec capabilities', 'format supported:', 'hwaccel')):
                                selected.append(line)
                        # Never persist the frame-by-frame debug stream.
                        ferr.write(''.join(selected) + ''.join(tail))
                    error_reader = threading.Thread(target=collect_stderr, daemon=True)
                    error_reader.start()
                    proc._scev_gpu = attempt_hw
                    self._register_proc(proc)
                    try:
                        assert proc.stdout is not None
                        for line in proc.stdout:
                            if self._cancel.is_set():
                                raise Cancelled()
                            if line.startswith("out_time_us="):
                                try:
                                    t = int(line.split("=", 1)[1]) / 1e6
                                except ValueError:
                                    continue
                                if t >= 0:
                                    seg_progress_cb(seg_idx, t)
                        rc = proc.wait()
                    finally:
                        if proc.poll() is None:
                            try:
                                proc.kill()
                                proc.wait()
                            except Exception:
                                pass
                        error_reader.join()
                        if proc.stdout:
                            proc.stdout.close()
                        if proc.stderr:
                            proc.stderr.close()
                        self._unregister_proc(proc)

                expected = {'cuda':'cuda','d3d11va':'d3d11','dxva2':'dxva2_vld',
                    'qsv':'qsv','vaapi':'vaapi','videotoolbox':'videotoolbox_vld'}.get(self.tools.hwaccel)
                verified = bool(expected and observed_formats and observed_formats[-1] == expected)
                if rc == 0 and attempt_hw and not verified:
                    rc = -1
                    with open(err_path,'a',encoding='utf8') as f:
                        f.write('\nHardware pixel format was not confirmed; retry on CPU.\n')
                if attempt_hw:
                    with self._proc_lock:
                        self._gpu_pending.discard(seg_idx)

                self._check()
                if rc == 0:
                    with open(err_path, encoding='utf-8', errors='replace') as f:
                        evidence = [line.strip() for line in f if any(k in line.lower() for k in
                            ('hwaccel', 'nvdec', 'd3d11', 'dxva', 'hardware decoding', 'chosen by get_format'))][-12:]
                    self.execution.append({"segment": seg_idx, "decoder_evidence": evidence,
                        "decoder": self.tools.hwaccel if attempt_hw else "cpu",
                        "hardware_frames_verified": verified if attempt_hw else False,
                        "transfer": 'FFmpeg decoder transfers hardware frames to CPU once' if attempt_hw else None,
                        "decode_threads": threads_per_seg,
                        "filter_threads": 1,
                        "vmaf_threads": threads_per_seg if do_cambi else 0,
                        "cambi_only": cambi_only if do_cambi else None})
                    if not self._tuning:
                        self.progress("decoder", 0, "Dekoder: " + (self.tools.hwaccel if attempt_hw else "CPU"))
                    break
                if not attempt_hw or not allow_fallback:
                    with open(err_path, "r", encoding="utf-8", errors="replace") as f:
                        err = f.read()[-1500:]
                    if allow_fallback and do_cambi and cambi_only:
                        self.tools.cambi_only = False
                        self.progress('decoder', 0, 'CAMBI-only nie działa dla tego fragmentu; powrót do libvmaf z modelem.')
                        return self._run_pixel_segment(path, info, seg_idx, seg_start,
                            seg_dur, do_siti, do_cambi, n_sub, threads_per_seg, False,
                            seg_progress_cb, skip_frames, output_start, cambi_frame_offset,
                            frame_count, allow_fallback, cambi_phase)
                    raise AnalysisError("Błąd ffmpeg podczas dekodowania:\n" + err)

            seg_siti = []
            sp = os.path.join(tmp, "siti.txt")
            if do_siti and os.path.isfile(sp):
                t = si = None
                with open(sp, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if line.startswith("frame:"):
                            m = re.search(r"pts_time:(-?[\d.]+)", line)
                            t = float(m.group(1)) if m else None
                        elif line.startswith("lavfi.siti.si="):
                            si = float(line.split("=", 1)[1])
                        elif line.startswith("lavfi.siti.ti="):
                            ti_val = float(line.split("=", 1)[1])
                            seg_siti.append({"t": t, "si": si, "ti": ti_val})
                if seg_siti:
                    t0 = seg_siti[0]["t"] if seg_siti[0]["t"] is not None else 0.0
                    for i, s in enumerate(seg_siti):
                        rel_t = (s["t"] - t0) if s["t"] is not None else (i / info["fps"])
                        base = output_start if output_start is not None else seg_start + skip_frames / info['fps']
                        s["t"] = round(base + max(0.0, rel_t), 4)
                        # Packet PTS are the canonical global timeline. Adding
                        # two rounded local timestamps caused a 0.1 ms drift
                        # depending on the chosen segment boundary.
                        origin = cambi_frame_offset if cambi_frame_offset is not None else (0 if seg_start == 0 else None)
                        if origin is not None and self._frame_times and origin+i < len(self._frame_times):
                            s['t'] = self._frame_times[origin+i]
                    if len(seg_siti) > 1 and not skip_frames:
                        seg_siti[0]["ti"] = seg_siti[1]["ti"]

            seg_cambi = []
            cp = os.path.join(tmp, "cambi.json")
            if do_cambi and os.path.isfile(cp):
                with open(cp, "r", encoding="utf-8") as f:
                    d = json.load(f)
                for fr in d.get("frames", []):
                    c = fr.get("metrics", {}).get("cambi")
                    if c is not None:
                        rel_t = fr.get("frameNum", 0) / info["fps"]
                        seg_cambi.append({
                            "t": round(((cambi_frame_offset + cambi_phase) / info['fps'] if cambi_frame_offset is not None else seg_start + cambi_skip / info['fps']) + rel_t, 4),
                            "cambi": float(c),
                        })
            return seg_siti, seg_cambi
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def pixel_pass(self, path, info):
        do_cambi = self.use_cambi and self.tools.has_libvmaf
        do_siti = self.tools.has_siti
        if not do_cambi and not do_siti:
            return [], [], None

        nb = max(info["nb_frames"], 1)
        n_sub = self.cambi_subsample or max(1, int(round(nb / 400.0)))
        dur = info["duration"] or (nb / info["fps"])
        use_hw = bool(self.sc.get("use_hwaccel", True))
        if not self._tuning:
            self._select_backend(path, info)
        use_hw = use_hw and self.tools.hwaccel is not None

        selected = max(1, min(6, int(self.sc.get('parallel_segments') or self.selected_segments)))
        if selected != self.selected_segments:
            self.selected_threads = max(1, (CPU_COUNT-2-selected)//(2*selected))
        available = perf.available_memory()
        # Conservative decoder/filter buffer budget, leaving half of free RAM.
        estimate = max(512*1024**2, info['width']*info['height']*(2 if info['bitdepth']>8 else 1)*96)
        if available:
            selected = min(selected, max(1, int(available*.5)//estimate))
        if selected > 1 and not self._tuning:
            while not self._frames_ready.wait(.1):
                self._check()
        if selected > 1 and self._frame_times and dur >= selected*4:
            siti, cambi = self.continuous_segments(path, info, selected, n_sub, self.selected_threads)
            return siti, cambi, n_sub if do_cambi else None
        # Without trusted packet timestamps / a validated tuning result,
        # keep a continuous decode and the global CAMBI sampling phase.
        n_segs = 1
        threads_per_seg = max(1, min(int(self.sc.get("threads_per_process") or 8), CPU_COUNT - 2))
        seg_len = (dur / n_segs) if (dur > 0 and n_segs > 1) else None

        seg_times = [0.0] * n_segs
        prog_lock = threading.Lock()
        last_ui = [0.0]
        hw_tag = f"GPU:{self.tools.hwaccel}" if (use_hw and self.tools.hwaccel) else "CPU"
        label = f"Dekodowanie ({hw_tag}, {n_segs}x): SI/TI" + (" + CAMBI" if do_cambi else "")

        def on_seg_progress(idx: int, t_local: float):
            if dur <= 0:
                return
            with prog_lock:
                max_seg = seg_len if seg_len is not None else dur
                seg_times[idx] = min(t_local, max_seg)
                now = time.monotonic()
                if now - last_ui[0] < 0.15:
                    return
                last_ui[0] = now
                done_t = sum(seg_times)
                frac = min(done_t / dur, 1.0)
            msg = f"{label}: {done_t:6.1f} / {dur:.1f} s"
            self._report_parallel_progress("pixels", frac, msg)

        if n_segs == 1:
            siti, cambi = self._run_pixel_segment(
                path, info, 0, 0.0, None,
                do_siti, do_cambi, n_sub, threads_per_seg, use_hw, on_seg_progress,
            )
            return siti, cambi, n_sub if do_cambi else None

    def sampled_pixel_pass(self, path, info, frames):
        """Sample continuous sequences; QP and packet size guide hard excerpts."""
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

    def continuous_segments(self, path, info, count, n_sub, threads, duration=None):
        """CFR segments with one preceding frame and a global CAMBI phase.

        Only used after an exact comparison against a continuous decode.
        VFR sources keep a single continuous decode.
        """
        fps = info['fps']
        total_frames = round(duration*fps) if duration else info['nb_frames']
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
                            info.get('storage_root'), self.sc.get('threads_per_process'), perf.ENGINE_REVISION])

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
        try:
            perf.atomic_json(cp, {'backend': self.tools.hwaccel, 'segments': self.selected_segments,
                                 'threads': self.selected_threads, 'benchmark': report})
        except OSError:
            pass

    def benchmark(self, path, info=None, seconds=6.0, full=True, automatic=False):
        """Runs original compressed input through real filters; no video files."""
        info = info or self.probe(path)
        self._verify_cambi_only()
        if full and not self._frames_ready.is_set():
            if getattr(self, '_analysis_active', False):
                while not self._frames_ready.wait(.1):
                    self._check()
            else:
                self._headers(path, info)
        saved_hw, saved_execution = self.tools.hwaccel, self.execution
        self.execution = []
        self._tuning = True
        rows = []
        reference = None
        deadline = time.monotonic() + 30 if automatic else float('inf')
        nsub = self.cambi_subsample or max(1, round(info['nb_frames'] / 400))
        try:
            backends = [None] + self.tools.hwaccels
            if automatic:
                order = ('cuda', 'd3d11va', 'videotoolbox', 'vaapi', 'qsv') if getattr(self, '_gpu_identity', '') else ('d3d11va', 'videotoolbox', 'vaapi', 'qsv', 'cuda')
                preferred = next((x for x in order if x in self.tools.hwaccels), None)
                backends = [None] + ([preferred] if preferred else [])
            for backend in backends:
                self.tools.hwaccel = backend
                start = time.perf_counter()
                try:
                    s, c = self._run_pixel_segment(path, info, 0, 0,
                        min(seconds, info['duration']) if info['duration'] else seconds,
                        self.tools.has_siti, self.use_cambi and self.tools.has_libvmaf,
                        nsub, 8, backend is not None, lambda *a: None, allow_fallback=False)
                    elapsed = time.perf_counter() - start
                    actual = self.execution[-1]['decoder']
                    if reference is None:
                        reference = (s, c)
                    equal = (s, c) == reference
                    rows.append({'backend': backend or 'cpu', 'actual': actual,
                                 'seconds': elapsed, 'equivalent': equal,
                                 'valid': equal and actual == (backend or 'cpu')})
                except AnalysisError as e:
                    rows.append({'backend': backend or 'cpu', 'valid': False, 'error': str(e)[-500:]})
                    if automatic and backend:
                        fallback_backend = next((x for x in order if x in self.tools.hwaccels and x not in backends), None)
                        if fallback_backend and time.monotonic() < deadline:
                            backends.append(fallback_backend)
            valid = [r for r in rows if r['valid']]
            winner = min(valid, key=lambda r: r['seconds'])['backend'] if valid else 'cpu'
            diagnostics = []
            if full:
                self.tools.hwaccel = None if winner == 'cpu' else winner
                for name, si, ca in ([] if automatic else [('siti', True, False), ('cambi', False, True)]):
                    if (si and not self.tools.has_siti) or (ca and not self.tools.has_libvmaf):
                        continue
                    start = time.perf_counter()
                    self._run_pixel_segment(path, info, 0, 0, seconds, si, ca, nsub, 8,
                                            self.tools.hwaccel is not None, lambda *a: None)
                    diagnostics.append({'metric': name, 'backend': winner, 'seconds': time.perf_counter()-start})
                candidate_backends = [r['backend'] for r in valid]
                if automatic:
                    candidate_backends.sort(key=lambda name: name == 'cpu')
                for candidate in candidate_backends:
                    self.tools.hwaccel = None if candidate == 'cpu' else candidate
                    for count in (2, 3, 4, 6):
                        if time.monotonic() > deadline:
                            break
                        available = perf.available_memory()
                        estimate = max(512*1024**2,info['width']*info['height']*(2 if info['bitdepth']>8 else 1)*96)
                        if available and count*estimate > available*.5:
                            continue
                        threads = max(1, (CPU_COUNT-2-count)//(2*count))
                        start = time.perf_counter()
                        before = len(self.execution)
                        vals = self.continuous_segments(path, info, count, nsub, threads, seconds)
                        elapsed = time.perf_counter()-start
                        decoders = {x['decoder'] for x in self.execution[before:]}
                        diagnostics.append({'backend': candidate, 'processes': count,
                            'threads_each': threads, 'seconds': elapsed,
                            'aggregate_source_fps': seconds*info['fps']/elapsed,
                            'equivalent': vals == reference and decoders == {candidate}})
            candidates = [(r['seconds'],1,8,r['backend']) for r in valid] + [
                (r['seconds'],r['processes'],r['threads_each'],r['backend'])
                for r in diagnostics if r.get('equivalent') and 'processes' in r]
            best = min(candidates) if candidates else (float('inf'),1,8,'cpu')
            winner = best[3]
            report = {'file': path, 'winner': winner, 'backends': rows,
                      'diagnostics': diagnostics, 'cambi_only': self.tools.cambi_only,
                      'recommended_segments': best[1], 'recommended_threads': best[2],
                      'seconds_sampled': seconds}
            if full:
                cp = perf.data_dir() / ('backend-' + self._backend_key(info) + '.json')
                try:
                    perf.atomic_json(cp, {'backend': None if winner=='cpu' else winner,
                                          'segments': best[1], 'threads': best[2], 'benchmark': report})
                except OSError:
                    pass
            return report
        finally:
            self.tools.hwaccel = saved_hw
            self.execution = saved_execution
            self._tuning = False

    def score(self, info, frames, siti, cambi):
        sc = self.sc
        codec = info["codec"]
        pixels = max(info["width"] * info["height"], 1)
        eff = sc["codec_efficiency"].get(codec, 1.0)
        win = float(sc["window_seconds"])
        dur = max((frames[-1]["t"] if frames else 0) + 1.0 / info["fps"], win)
        n_win = max(1, int(math.ceil(dur / win - 1e-9)))

        def bucket(items, key):
            b = [[] for _ in range(n_win)]
            for it in items:
                v = it.get(key)
                if v is None or it.get("t") is None:
                    continue
                i = min(int(it["t"] // win), n_win - 1)
                b[i].append(v)
            return b

        b_qp = bucket(frames, "qp")
        b_size = bucket(frames, "size")
        b_si = bucket(siti, "si")
        b_ti = bucket(siti, "ti")
        b_cambi = bucket(cambi, "cambi")
        g_si = mean([s["si"] for s in siti])
        g_ti = mean([s["ti"] for s in siti])
        g_cambi = mean([c["cambi"] for c in cambi])
        has_cambi = bool(cambi)
        has_siti = bool(siti)

        w = {
            "qp": float(sc["weights"].get("qp", 0.7)),
            "bits": float(sc["weights"].get("bits", 0.3)),
        }
        if all(not q for q in b_qp):
            w["qp"] = 0.0
        tot = sum(w.values()) or 1.0
        w = {k: v / tot for k, v in w.items()}

        windows = []
        for i in range(n_win):
            qp = mean(b_qp[i])
            nfr = len(b_size[i])
            bpp = (sum(b_size[i]) * 8.0 / (pixels * nfr)) if nfr else None
            si = mean(b_si[i]) if b_si[i] else g_si
            ti = mean(b_ti[i]) if b_ti[i] else g_ti
            cam = mean(b_cambi[i]) if b_cambi[i] else g_cambi

            s_qp = interp(sc["qp_curve"], qp) if qp is not None else None
            s_bits = None
            if bpp is not None:
                if has_siti and si is not None:
                    req = (
                        sc["bits_req_base"]
                        * (max(si, 5.0) / sc["bits_si_ref"]) ** sc["bits_si_exp"]
                        * (sc["bits_ti_offset"] + (ti or 0.0) / sc["bits_ti_ref"])
                    )
                else:
                    req = sc["bits_req_base"] * 1.5
                ratio = bpp * eff / max(req, 1e-6)
                s_bits = 100.0 * (1.0 - math.exp(-sc["bits_k"] * ratio))
            s_cam = interp(sc["cambi_curve"], cam) if (has_cambi and cam is not None) else None

            parts = {"qp": s_qp, "bits": s_bits}
            num = sum(w[k] * v for k, v in parts.items() if v is not None)
            den = sum(w[k] for k, v in parts.items() if v is not None)
            if den <= 0:
                continue
            s = num / den
            used = [v for k, v in parts.items() if v is not None and w[k] > 0]
            if used:
                s = min(s, min(used) + sc["cap_margin"])
            if s_cam is not None:
                pen = (100.0 - s_cam) * sc["cambi_penalty"]
                s = min(s - pen, s_cam + sc["cambi_cap_margin"])
            windows.append({
                "t": round(i * win, 3),
                "qp": qp,
                "bpp": bpp,
                "si": si,
                "ti": ti,
                "cambi": cam,
                "s_qp": s_qp,
                "s_bits": s_bits,
                "s_cambi": s_cam,
                "score": max(0.0, min(100.0, s)),
            })

        if not windows:
            raise AnalysisError("Brak danych do oceny.")
        scores = [x["score"] for x in windows]
        m = mean(scores)
        p5 = percentile(scores, 5)
        overall = sc["mean_weight"] * m + (1 - sc["mean_weight"]) * p5
        return windows, {
            "overall": round(overall, 1),
            "mean": round(m, 1),
            "p5": round(p5, 1),
            "worst_window_t": min(windows, key=lambda x: x["score"])["t"],
            "qp": _r(mean([x["s_qp"] for x in windows])),
            "bits": _r(mean([x["s_bits"] for x in windows])),
            "cambi": _r(mean([x["s_cambi"] for x in windows])),
            "weights": {k: round(v, 3) for k, v in w.items()},
            "grade": grade_for(overall),
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


def _r(x, n=1):
    return None if x is None else round(x, n)


def export_csv(result, path):
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["# plik", result["file"]])
        w.writerow(["# ocena", result["scores"]["overall"], result["scores"]["grade"]])
        w.writerow(["# tryb", result.get('mode', 'balanced'),
                    'PRZYBLIŻONY' if result.get('approximate') else 'pełny'])
        w.writerow(["# dekoder", result['stats'].get('decoder', '')])
        w.writerow(["# cache", result.get('cache_hit', False)])
        w.writerow([])
        w.writerow([
            "okno_s", "ocena", "ocena_qp", "ocena_bitrate",
            "ocena_cambi", "qp_ekw", "bpp", "SI", "TI", "CAMBI",
        ])
        for x in result["windows"]:
            w.writerow([
                x["t"], _r(x["score"]), _r(x["s_qp"]), _r(x["s_bits"]),
                _r(x["s_cambi"]), _r(x["qp"], 2), _r(x["bpp"], 4),
                _r(x["si"], 2), _r(x["ti"], 2), _r(x["cambi"], 3),
            ])
        w.writerow([])
        w.writerow(["czas_s", "typ", "rozmiar_B", "qp_surowe", "qp_ekw"])
        for x in result["frames"]:
            w.writerow([
                x["t"], x["type"], x["size"], _r(x["qp_raw"], 2), _r(x["qp"], 2),
            ])


def cli(argv):
    import argparse
    ap = argparse.ArgumentParser(
        prog=APP_NAME, description="Ocena jakości kompresji (no-reference)"
    )
    ap.add_argument("files", nargs="+")
    ap.add_argument("--no-cambi", action="store_true")
    ap.add_argument("--mode", choices=["fast", "balanced", "accurate"], default="balanced")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--benchmark", action="store_true", help="diagnostyka dekoderów i filtrów")
    ap.add_argument("--subsample", type=int, default=None)
    ap.add_argument("--ffmpeg-dir", default=None)
    ap.add_argument("--json", default=None, help="zapisz pełny wynik do JSON")
    a = ap.parse_args(argv)
    tools = Tools(a.ffmpeg_dir)
    print(tools.describe())
    for fp in a.files:
        an = Analyzer(tools, use_cambi=not a.no_cambi, cambi_subsample=a.subsample, mode=a.mode, force=a.force)
        if a.benchmark:
            benchmark = an.benchmark(fp)
            print(json.dumps(benchmark, indent=2, ensure_ascii=False))
            if a.json:
                perf.atomic_json(a.json, benchmark)
                a.json = None
            continue
        try:
            r = an.analyze(fp)
        except AnalysisError as e:
            print(f"{fp}: BŁĄD {e}")
            continue
        s, st, inf = r["scores"], r["stats"], r["info"]
        print(
            f"\n{r['name']}: {s['overall']} ({s['grade']})  "
            f"śr={s['mean']} p5={s['p5']}  [{st['analysis_seconds']}s]"
        )
        print(f"  Dekoder: {st.get('decoder', '?')}; {st.get('analysis_fps', 0):.1f} FPS; "
              f"cache: {'tak' if r.get('cache_hit') else 'nie'}; tryb: {r.get('mode', 'balanced')}"
              + (' (PRZYBLIŻONY)' if r.get('approximate') else ''))
        print(
            f"  {inf['codec']} {inf['width']}x{inf['height']} "
            f"{inf['fps']:.2f}fps {inf['bitdepth']}bit "
            f"{st['bitrate_mbps']} Mb/s  bpp={st['bpp']}"
        )
        print(
            f"  QP: {st['qp_raw_mean']} ({st['qp_scale']}), "
            f"ekw. {st['qp_eq_mean']}  -> {s['qp']}"
        )
        print(
            f"  bitrate/złożoność: SI={st['si_mean']} "
            f"TI={st['ti_mean']}  -> {s['bits']}"
        )
        print(
            f"  CAMBI: śr={st['cambi_mean']} "
            f"max={st['cambi_max']}  -> {s['cambi']}"
        )
        for wmsg in r["warnings"]:
            print("  ! " + wmsg)
        if a.json:
            with open(a.json, "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=1)
            a.json = None


if __name__ == "__main__":
    cli(sys.argv[1:])
