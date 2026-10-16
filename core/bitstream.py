from __future__ import annotations

import subprocess
import time

from .common import AnalysisError, Cancelled, NO_WINDOW, RE_PACKET, RE_FIELD, mean

class BitstreamMixin:
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
