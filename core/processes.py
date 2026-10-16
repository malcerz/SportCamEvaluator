from __future__ import annotations

import math
import subprocess
import time

from .common import Cancelled

class ProcessMixin:
    def _begin_pixel_progress(self):
        with self._prog_lock:
            if not self._pix_active:
                self._pix_active = True
                self._fps_sample = None
                self._fps_smoothed = None

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
            now = time.monotonic()
            info = getattr(self, "_progress_info", None)
            started = getattr(self, "_analysis_started", now)
            if started is None:
                started = now
            frame_count = int((info or {}).get("nb_frames") or 0)
            # A frame is complete only when both full-frame passes have reached it.
            completed_frac = min(self._bit_frac, self._pix_frac) if self._pix_active else self._bit_frac
            completed = min(frame_count, max(0, round(completed_frac * frame_count)))
            previous = getattr(self, "_fps_sample", None)
            if previous:
                sample_time, sample_frames = previous
                dt = now - sample_time
                df = completed - sample_frames
                if dt >= 0.35 and df > 0:
                    rate = df / dt
                    alpha = 1.0 - math.exp(-dt / 6.0)
                    old = getattr(self, "_fps_smoothed", None)
                    self._fps_smoothed = rate if old is None else old + alpha * (rate - old)
                    self._fps_sample = (now, completed)
            else:
                self._fps_sample = (now, completed)

            elapsed = max(0.0, now - started)
            average = completed / elapsed if elapsed > 0 else 0.0
            smooth = getattr(self, "_fps_smoothed", None)
            remaining = max(0, frame_count - completed)
            if smooth and elapsed >= 8 and completed_frac >= 0.02:
                seconds = int(round(remaining / smooth))
                eta = f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02d}"
                current_text = f"{smooth:.1f} FPS chwilowo"
                eta_text = f"ETA {eta}"
            else:
                current_text = "FPS chwilowo —"
                eta_text = "ETA —"
            avg_text = f"{average:.1f} avg" if elapsed >= 8 else "avg —"
            with self._proc_lock:
                active = sum(1 for p in self._procs if p.poll() is None)
            decoder = next((x.get("decoder") for x in reversed(self.execution) if x.get("decoder")), None)
            decoder = decoder or getattr(self.tools, "hwaccel", None) or "CPU"
            decoder_label = "NVDEC/CUDA" if decoder == "cuda" else decoder.upper() if decoder != "cpu" else "CPU"
            detail = (f"{completed:,}/{frame_count:,} kl. (+{remaining:,}) · {total:.0%} · "
                      f"{current_text} · {avg_text} · {eta_text} · {decoder_label} · {active} proc.")
            self.progress("analysis", min(total * 0.98, 0.98), f"{msg} · {detail}")
