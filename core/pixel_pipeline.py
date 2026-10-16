from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import performance as perf
from .common import AnalysisError, Cancelled, CPU_COUNT, NO_WINDOW

class PixelPipelineMixin:
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
        stage: str = "combined",
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
        if stage == "decode":
            fg = f"{src}null[o]"
        elif stage == "convert":
            fg = f"{src}format={pix_target},null[o]"
        elif stage == "siti":
            do_siti, do_cambi = True, False
            fg = f"{src}{siti_chain}[o]"
        elif stage == "cambi":
            do_siti, do_cambi = False, True
            fg = f"{src}format={pix_target}," + (f"trim=start_frame={cambi_skip}," if cambi_skip else "") + f"split=2[a][b];[a][b]{vmaf_opts}"
        elif do_cambi and do_siti:
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
            loglevel = self.hw_loglevel if hw_enabled else "error"
            cmd = [self.tools.ffmpeg, "-hide_banner", "-nostats", "-loglevel", loglevel,
                   "-filter_complex_threads", str(self.filter_complex_threads)]
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
                    stderr_bytes = [0]
                    def collect_stderr():
                        tail = deque(maxlen=80)
                        selected = deque(maxlen=24)
                        for line in proc.stderr:
                            stderr_bytes[0] += len(line)
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
                hw_format_verified = bool(expected and observed_formats and observed_formats[-1] == expected)
                verified = hw_format_verified
                trusted_calibration = bool(attempt_hw and self.trust_hw_for_benchmark
                                           and self.hw_loglevel != "debug")
                verified = verified or trusted_calibration
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
                        "hardware_frames_verified": hw_format_verified if attempt_hw else False,
                        "hardware_validation": "calibrated" if trusted_calibration else "log evidence",
                        "transfer": 'FFmpeg decoder transfers hardware frames to CPU once' if attempt_hw else None,
                        "decode_threads": threads_per_seg,
                        "filter_threads": self.filter_complex_threads,
                        "vmaf_threads": threads_per_seg if do_cambi else 0,
                        "cambi_only": cambi_only if do_cambi else None,
                        "loglevel": self.hw_loglevel if attempt_hw else "error",
                        "stderr_bytes": stderr_bytes[0],
                        "stage": stage})
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
                            frame_count, allow_fallback, cambi_phase, stage)
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
        self._begin_pixel_progress()
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
        self.filter_complex_threads = max(1, int(self.sc.get('filter_complex_threads') or self.selected_filter_threads))
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
