"""
SportCamEvaluator - silnik analizy.

Ocena jakości kompresji pliku wideo BEZ dostępu do źródła (no-reference).
Wykorzystuje wyłącznie ffmpeg/ffprobe:

  1. Przebieg "bitstream" (bez dekodowania pikseli, filtr bsf trace_headers):
       - QP każdej ramki (H.264 / H.265: slice QP, AV1 / VP9: base_q_idx)
       - rozmiar i typ każdej ramki
  2. Przebieg "piksele" (dekodowanie):
       - SI/TI (ITU-T P.910) - złożoność treści, do normalizacji bitrate
       - CAMBI (Netflix, libvmaf) - banding / pasmowanie w gradientach

Na tej podstawie liczone są trzy oceny cząstkowe (0-100) w oknach 1 s
i ocena końcowa. Wszystkie progi są heurystyczne i można je nadpisać
plikiem scoring.json leżącym obok programu.
"""

from __future__ import annotations

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

APP_NAME = "SportCamEvaluator"
APP_VERSION = "1.0.0"

IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

# ----------------------------------------------------------------------------
# Domyślne parametry oceny (heurystyka - patrz README "Jak liczona jest ocena")
# ----------------------------------------------------------------------------
DEFAULT_SCORING = {
    # QP (w skali H.265/H.264) -> ocena. Pary [qp, ocena], interpolacja liniowa.
    "qp_curve": [[0, 100], [20, 100], [26, 95], [30, 90], [34, 83], [38, 73],
                 [42, 60], [46, 42], [51, 15]],
    # AV1/VP9: base_q_idx (0-255) -> QP-ekwiwalent H.265 = a + b * q_idx
    #   (dopasowane do VMAF: q_idx 120 ~ QP 28, 250 ~ QP 43)
    "qindex_to_qp_a": 14.0,
    "qindex_to_qp_b": 0.116,
    # H.264 przy tym samym QP wygląda gorzej niż H.265 - przesunięcie do skali H.265
    "h264_qp_offset": 3.0,
    # Względna wydajność kodeków (ile razy mniej bitów niż H.264 przy tej samej jakości)
    "codec_efficiency": {"h264": 1.0, "hevc": 1.5, "vp9": 1.4, "av1": 1.9},
    # Bitrate: wymagane bpp (H.264-ekwiwalent) dla "przezroczystej" jakości
    #   req = base * (SI/si_ref)^si_exp * (ti_offset + TI/ti_ref)
    "bits_req_base": 0.10,
    "bits_si_ref": 60.0,
    "bits_si_exp": 0.7,
    "bits_ti_ref": 20.0,
    "bits_ti_offset": 0.5,
    # ocena = 100 * (1 - exp(-k * bpp_eff / req))
    "bits_k": 1.6,
    # CAMBI (0..24, 0 = brak bandingu) -> ocena
    "cambi_curve": [[0, 100], [1, 100], [3, 85], [5, 65], [8, 40], [12, 15], [20, 0]],
    # wagi ocen bazowych (QP i bitrate); CAMBI działa jako kara, nie jako składnik średniej
    "weights": {"qp": 0.70, "bits": 0.30},
    # ocena bazowa nie może przekroczyć gorszej z ocen QP/bitrate + cap_margin
    "cap_margin": 25.0,
    # kara za banding: ocena -= (100 - ocena_CAMBI) * cambi_penalty, max = ocena_CAMBI + cambi_cap_margin
    "cambi_penalty": 0.6,
    "cambi_cap_margin": 20.0,
    # ocena końcowa = mean_w * średnia + (1-mean_w) * 5. percentyl okien
    "mean_weight": 0.75,
    "window_seconds": 1.0,
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


# ----------------------------------------------------------------------------
# Lokalizacja programu i narzędzi
# ----------------------------------------------------------------------------
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
        cands += [os.path.join(configured_dir, exe), os.path.join(configured_dir, "bin", exe)]
    ad = app_dir()
    cands += [os.path.join(ad, exe), os.path.join(ad, "ffmpeg", exe),
              os.path.join(ad, "ffmpeg", "bin", exe)]
    ffd = os.path.join(ad, "ffmpeg")
    if os.path.isdir(ffd):
        for sub in sorted(os.listdir(ffd)):
            cands.append(os.path.join(ffd, sub, "bin", exe))
    for c in cands:
        if os.path.isfile(c):
            return c
    return shutil.which(name)


def _run(cmd, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=NO_WINDOW)


class Tools:
    """Wykryte ffmpeg/ffprobe i ich możliwości."""

    def __init__(self, configured_dir: str | None = None):
        self.ffmpeg = find_tool("ffmpeg", configured_dir)
        self.ffprobe = find_tool("ffprobe", configured_dir)
        self.version = ""
        self.has_libvmaf = False
        self.has_siti = False
        self.has_trace = False
        if self.ffmpeg:
            try:
                v = _run([self.ffmpeg, "-hide_banner", "-version"]).stdout
                self.version = v.splitlines()[0] if v else ""
                flt = _run([self.ffmpeg, "-hide_banner", "-filters"]).stdout
                self.has_libvmaf = re.search(r"\slibvmaf\s", flt) is not None
                self.has_siti = re.search(r"\ssiti\s", flt) is not None
                bsf = _run([self.ffmpeg, "-hide_banner", "-bsfs"]).stdout
                self.has_trace = "trace_headers" in bsf
            except Exception:
                pass

    @property
    def ok(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe and self.has_trace)

    def describe(self) -> str:
        if not self.ffmpeg or not self.ffprobe:
            return "Nie znaleziono ffmpeg/ffprobe"
        parts = [self.version.replace("ffmpeg version ", "ffmpeg ")[:40]]
        parts.append("CAMBI: tak" if self.has_libvmaf else "CAMBI: brak libvmaf")
        if not self.has_siti:
            parts.append("brak filtra siti")
        return " | ".join(parts)


# ----------------------------------------------------------------------------
# Pomocnicze
# ----------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------
# Analiza
# ----------------------------------------------------------------------------
RE_PACKET = re.compile(r"Packet: (\d+) bytes(, key frame)?.*?, pts (-?\d+|NOPTS|N/A)")
RE_FIELD = re.compile(r"\]\s+\d+\s+([A-Za-z_][\w\[\]]*)\s+\S*\s*=\s*(-?\d+)\s*$")

SUPPORTED_QP = {"h264", "hevc", "av1", "vp9"}


class Analyzer:
    def __init__(self, tools: Tools, scoring: dict | None = None,
                 use_cambi: bool = True, cambi_subsample: int | None = None,
                 progress=None):
        self.tools = tools
        self.sc = scoring or load_scoring()
        self.use_cambi = use_cambi
        self.cambi_subsample = cambi_subsample
        self.progress = progress or (lambda phase, frac, msg: None)
        self._cancel = threading.Event()
        self._proc: subprocess.Popen | None = None

    def cancel(self):
        self._cancel.set()
        p = self._proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def _check(self):
        if self._cancel.is_set():
            raise Cancelled()

    # ---------------------------------------------------------------- probe
    def probe(self, path):
        r = _run([self.tools.ffprobe, "-v", "error", "-show_streams", "-show_format",
                  "-of", "json", path], timeout=120)
        if r.returncode != 0:
            raise AnalysisError("ffprobe nie może odczytać pliku:\n" + r.stderr.strip()[-500:])
        data = json.loads(r.stdout or "{}")
        vs = [s for s in data.get("streams", []) if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
        if not vs:
            raise AnalysisError("Plik nie zawiera strumienia wideo.")
        s = vs[0]
        fmt = data.get("format", {})
        fps = _frac(s.get("avg_frame_rate", "0/0")) or _frac(s.get("r_frame_rate", "0/0")) or 30.0
        dur = float(s.get("duration") or fmt.get("duration") or 0) or 0.0
        pix = s.get("pix_fmt", "") or ""
        bitdepth = 12 if "12" in pix else 10 if "10" in pix else 8
        try:
            bitdepth = int(s.get("bits_per_raw_sample") or bitdepth)
        except ValueError:
            pass
        nb = int(s.get("nb_frames") or 0) or int(round(dur * fps)) or 0
        return {
            "index": s["index"],
            "codec": s.get("codec_name", "?"),
            "codec_long": s.get("codec_long_name", ""),
            "profile": s.get("profile", ""),
            "width": int(s.get("width", 0)),
            "height": int(s.get("height", 0)),
            "pix_fmt": pix,
            "bitdepth": bitdepth,
            "fps": fps,
            "time_base": _frac(s.get("time_base", "1/1000")),
            "duration": dur,
            "nb_frames": nb,
            "stream_bitrate": float(s.get("bit_rate") or 0),
            "container": fmt.get("format_long_name", fmt.get("format_name", "")),
            "file_size": int(fmt.get("size") or os.path.getsize(path)),
        }

    # ------------------------------------------------------- pass 1: bitstream
    def bitstream_pass(self, path, info):
        codec = info["codec"]
        cmd = [self.tools.ffmpeg, "-hide_banner", "-nostats", "-i", path,
               "-map", f"0:{info['index']}", "-c", "copy",
               "-bsf:v", "trace_headers", "-f", "null", "-"]
        frames = []
        cur = None
        h_init = {}            # pps id -> init_qp (h264/hevc)
        last_pps = 0
        nb = max(info["nb_frames"], 1)
        tb = info["time_base"] or 1 / 1000.0
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                      text=True, encoding="utf-8", errors="replace",
                                      creationflags=NO_WINDOW, bufsize=1 << 16)
        tail = []
        last_ui = 0.0
        try:
            for line in self._proc.stderr:
                if self._cancel.is_set():
                    raise Cancelled()
                if "trace_headers" not in line:
                    tail.append(line)
                    if len(tail) > 30:
                        tail.pop(0)
                    continue
                m = RE_PACKET.search(line)
                if m:
                    pts = m.group(3)
                    cur = {"size": int(m.group(1)), "key": bool(m.group(2)),
                           "pts": int(pts) * tb if pts.lstrip("-").isdigit() else None,
                           "qps": [], "types": [], "shown": []}
                    frames.append(cur)
                    now = time.monotonic()
                    if now - last_ui > 0.2:
                        last_ui = now
                        self.progress("bitstream", min(len(frames) / nb, 1.0),
                                      f"Odczyt nagłówków: ramka {len(frames)}/{nb}")
                    continue
                m = RE_FIELD.search(line)
                if not m:
                    continue
                name, val = m.group(1), int(m.group(2))
                if codec in ("hevc", "h264"):
                    if name in ("pps_pic_parameter_set_id", "slice_pic_parameter_set_id",
                                "pic_parameter_set_id"):
                        last_pps = val
                    elif name in ("init_qp_minus26", "pic_init_qp_minus26"):
                        h_init[last_pps] = 26 + val
                    elif name == "slice_qp_delta" and cur is not None:
                        cur["qps"].append(h_init.get(last_pps, 26) + val)
                    elif name == "slice_type" and cur is not None:
                        if codec == "hevc":
                            cur["types"].append({0: "B", 1: "P", 2: "I"}.get(val, "?"))
                        else:
                            cur["types"].append({0: "P", 1: "B", 2: "I", 3: "P", 4: "I"}.get(val % 5, "?"))
                else:  # av1 / vp9
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
            self._proc.wait()
        finally:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
        self._check()
        if not frames:
            raise AnalysisError("Nie udało się odczytać pakietów wideo.\n" + "".join(tail[-8:]))

        # porządek wyświetlania (pts), konwersja QP
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
                t = "I" if "I" in f["types"] else ("B" if all(x == "B" for x in f["types"]) else "P")
            else:
                t = "I" if f["key"] else "P"
            out.append({"pts": f["pts"] if f["pts"] is not None else i / info["fps"],
                        "size": f["size"], "type": t, "qp_raw": qp_raw, "qp": qp_eq})
        out.sort(key=lambda x: x["pts"])
        t0 = out[0]["pts"]
        for f in out:
            f["t"] = round(f["pts"] - t0, 4)
            del f["pts"]
        return out

    # --------------------------------------------------------- pass 2: pixels
    def pixel_pass(self, path, info):
        do_cambi = self.use_cambi and self.tools.has_libvmaf
        do_siti = self.tools.has_siti
        if not do_cambi and not do_siti:
            return [], [], None
        nb = max(info["nb_frames"], 1)
        n_sub = self.cambi_subsample or max(1, int(round(nb / 400.0)))
        threads = max(1, min(os.cpu_count() or 2, 16))
        src = f"[0:{info['index']}]"
        siti_chain = ("scale=w='min(1280,iw)':h=-2:flags=bilinear,format=yuv420p,"
                      "siti,metadata=mode=print:file=siti.txt")
        if do_cambi and do_siti:
            fg = (f"{src}split=3[s][a][b];[s]{siti_chain},nullsink;"
                  f"[a][b]libvmaf=model=version=vmaf_v0.6.1:feature=name=cambi:"
                  f"log_fmt=json:log_path=cambi.json:n_subsample={n_sub}:n_threads={threads}[o]")
        elif do_cambi:
            fg = (f"{src}split=2[a][b];"
                  f"[a][b]libvmaf=model=version=vmaf_v0.6.1:feature=name=cambi:"
                  f"log_fmt=json:log_path=cambi.json:n_subsample={n_sub}:n_threads={threads}[o]")
        else:
            fg = f"{src}{siti_chain}[o]"

        tmp = tempfile.mkdtemp(prefix="scev_")
        err_path = os.path.join(tmp, "ffmpeg_err.txt")
        cmd = [self.tools.ffmpeg, "-hide_banner", "-nostats", "-i", path,
               "-filter_complex", fg, "-map", "[o]", "-f", "null", "-",
               "-progress", "pipe:1"]
        dur = info["duration"] or (nb / info["fps"])
        try:
            with open(err_path, "w", encoding="utf-8", errors="replace") as ferr:
                self._proc = subprocess.Popen(cmd, cwd=tmp, stdout=subprocess.PIPE, stderr=ferr,
                                              text=True, encoding="utf-8", errors="replace",
                                              creationflags=NO_WINDOW)
                label = "Dekodowanie: SI/TI" + (" + CAMBI" if do_cambi else "")
                for line in self._proc.stdout:
                    if self._cancel.is_set():
                        raise Cancelled()
                    if line.startswith("out_time_us="):
                        try:
                            t = int(line.split("=", 1)[1]) / 1e6
                        except ValueError:
                            continue
                        if dur > 0:
                            self.progress("pixels", min(t / dur, 1.0),
                                          f"{label}: {t:6.1f} / {dur:.1f} s")
                rc = self._proc.wait()
            self._check()
            if rc != 0:
                with open(err_path, "r", encoding="utf-8", errors="replace") as f:
                    err = f.read()[-1500:]
                raise AnalysisError("Błąd ffmpeg podczas dekodowania:\n" + err)

            siti = []
            sp = os.path.join(tmp, "siti.txt")
            if os.path.isfile(sp):
                t = si = None
                with open(sp, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if line.startswith("frame:"):
                            m = re.search(r"pts_time:(-?[\d.]+)", line)
                            t = float(m.group(1)) if m else None
                        elif line.startswith("lavfi.siti.si="):
                            si = float(line.split("=", 1)[1])
                        elif line.startswith("lavfi.siti.ti="):
                            siti.append({"t": t, "si": si, "ti": float(line.split("=", 1)[1])})
                if siti:
                    t0 = siti[0]["t"] or 0.0
                    for i, s in enumerate(siti):
                        s["t"] = round((s["t"] - t0) if s["t"] is not None else i / info["fps"], 4)
                    if len(siti) > 1:
                        siti[0]["ti"] = siti[1]["ti"]  # TI pierwszej ramki = 0 z definicji

            cambi = []
            cp = os.path.join(tmp, "cambi.json")
            if do_cambi and os.path.isfile(cp):
                with open(cp, "r", encoding="utf-8") as f:
                    d = json.load(f)
                for fr in d.get("frames", []):
                    c = fr.get("metrics", {}).get("cambi")
                    if c is not None:
                        cambi.append({"t": round(fr.get("frameNum", 0) / info["fps"], 4),
                                      "cambi": float(c)})
            return siti, cambi, n_sub if do_cambi else None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ----------------------------------------------------------------- scoring
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

        w = {"qp": float(sc["weights"].get("qp", 0.7)), "bits": float(sc["weights"].get("bits", 0.3))}
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
                    req = (sc["bits_req_base"]
                           * (max(si, 5.0) / sc["bits_si_ref"]) ** sc["bits_si_exp"]
                           * (sc["bits_ti_offset"] + (ti or 0.0) / sc["bits_ti_ref"]))
                else:
                    req = sc["bits_req_base"] * 1.5
                ratio = bpp * eff / max(req, 1e-6)
                s_bits = 100.0 * (1.0 - math.exp(-sc["bits_k"] * ratio))
            s_cam = interp(sc["cambi_curve"], cam) if (has_cambi and cam is not None) else None

            # baza: QP + bitrate; CAMBI działa wyłącznie jako kara (brak bandingu nie podnosi oceny)
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
                s = min(s - (100.0 - s_cam) * sc["cambi_penalty"], s_cam + sc["cambi_cap_margin"])
            windows.append({"t": round(i * win, 3), "qp": qp, "bpp": bpp, "si": si, "ti": ti,
                            "cambi": cam, "s_qp": s_qp, "s_bits": s_bits, "s_cambi": s_cam,
                            "score": max(0.0, min(100.0, s))})

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

    # -------------------------------------------------------------------- run
    def analyze(self, path):
        if not self.tools.ok:
            raise AnalysisError("Brak działającego ffmpeg/ffprobe (z filtrem trace_headers).")
        path = os.path.abspath(path)
        t_start = time.monotonic()
        warnings = []
        self.progress("probe", 0.0, "Odczyt informacji o pliku…")
        info = self.probe(path)
        self._check()
        if info["codec"] not in SUPPORTED_QP:
            warnings.append(f"Kodek {info['codec']} nieobsługiwany przy odczycie QP – "
                            "ocena tylko z bitrate i CAMBI.")
        frames = self.bitstream_pass(path, info)
        if info["codec"] in SUPPORTED_QP and all(f["qp"] is None for f in frames):
            warnings.append("Nie znaleziono wartości QP w nagłówkach strumienia.")
        self._check()
        siti, cambi, n_sub = self.pixel_pass(path, info)
        if self.use_cambi and not self.tools.has_libvmaf:
            warnings.append("ffmpeg bez libvmaf – CAMBI (banding) pominięte. "
                            "Użyj pełnej wersji ffmpeg (np. gyan.dev 'full' lub BtbN 'gpl').")
        if not self.tools.has_siti:
            warnings.append("ffmpeg bez filtra siti – bitrate oceniany bez normalizacji złożoności.")
        self._check()
        self.progress("score", 1.0, "Liczenie oceny…")
        windows, scores = self.score(info, frames, siti, cambi)

        sizes = [f["size"] for f in frames]
        qps_raw = [f["qp_raw"] for f in frames if f["qp_raw"] is not None]
        qps_eq = [f["qp"] for f in frames if f["qp"] is not None]
        dur = info["duration"] or (len(frames) / info["fps"])
        i_idx = [i for i, f in enumerate(frames) if f["type"] == "I"]
        gop = mean([b - a for a, b in zip(i_idx, i_idx[1:])]) if len(i_idx) > 1 else None
        stats = {
            "frames": len(frames),
            "count_I": sum(1 for f in frames if f["type"] == "I"),
            "count_P": sum(1 for f in frames if f["type"] == "P"),
            "count_B": sum(1 for f in frames if f["type"] == "B"),
            "gop_avg": _r(gop),
            "bitrate_mbps": _r(sum(sizes) * 8 / dur / 1e6, 2) if dur else None,
            "bpp": _r(sum(sizes) * 8 / (len(frames) * info["width"] * info["height"]), 4),
            "frame_size_avg": _r(mean(sizes), 0),
            "frame_size_max": max(sizes),
            "qp_raw_mean": _r(mean(qps_raw)),
            "qp_raw_min": _r(min(qps_raw)) if qps_raw else None,
            "qp_raw_max": _r(max(qps_raw)) if qps_raw else None,
            "qp_eq_mean": _r(mean(qps_eq)),
            "qp_scale": ("base_q_idx 0-255" if info["codec"] in ("av1", "vp9") else "QP 0-51"),
            "si_mean": _r(mean([s["si"] for s in siti])),
            "ti_mean": _r(mean([s["ti"] for s in siti])),
            "cambi_mean": _r(mean([c["cambi"] for c in cambi]), 2),
            "cambi_max": _r(max([c["cambi"] for c in cambi]), 2) if cambi else None,
            "cambi_p95": _r(percentile([c["cambi"] for c in cambi], 95), 2),
            "cambi_subsample": n_sub,
            "analysis_seconds": round(time.monotonic() - t_start, 1),
        }
        return {
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


def _r(x, n=1):
    return None if x is None else round(x, n)


def export_csv(result, path):
    """CSV z danymi per ramka + oknami oceny."""
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["# plik", result["file"]])
        w.writerow(["# ocena", result["scores"]["overall"], result["scores"]["grade"]])
        w.writerow([])
        w.writerow(["okno_s", "ocena", "ocena_qp", "ocena_bitrate", "ocena_cambi",
                    "qp_ekw", "bpp", "SI", "TI", "CAMBI"])
        for x in result["windows"]:
            w.writerow([x["t"], _r(x["score"]), _r(x["s_qp"]), _r(x["s_bits"]), _r(x["s_cambi"]),
                        _r(x["qp"], 2), _r(x["bpp"], 4), _r(x["si"], 2), _r(x["ti"], 2),
                        _r(x["cambi"], 3)])
        w.writerow([])
        w.writerow(["czas_s", "typ", "rozmiar_B", "qp_surowe", "qp_ekw"])
        for x in result["frames"]:
            w.writerow([x["t"], x["type"], x["size"], _r(x["qp_raw"], 2), _r(x["qp"], 2)])


def cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog=APP_NAME, description="Ocena jakości kompresji (no-reference)")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--no-cambi", action="store_true")
    ap.add_argument("--subsample", type=int, default=None)
    ap.add_argument("--ffmpeg-dir", default=None)
    ap.add_argument("--json", default=None, help="zapisz pełny wynik (pierwszego pliku) do JSON")
    a = ap.parse_args(argv)
    tools = Tools(a.ffmpeg_dir)
    print(tools.describe())
    for fp in a.files:
        an = Analyzer(tools, use_cambi=not a.no_cambi, cambi_subsample=a.subsample)
        try:
            r = an.analyze(fp)
        except AnalysisError as e:
            print(f"{fp}: BŁĄD {e}")
            continue
        s, st, inf = r["scores"], r["stats"], r["info"]
        print(f"\n{r['name']}: {s['overall']} ({s['grade']})  śr={s['mean']} p5={s['p5']}")
        print(f"  {inf['codec']} {inf['width']}x{inf['height']} {inf['fps']:.2f}fps {inf['bitdepth']}bit "
              f"{st['bitrate_mbps']} Mb/s  bpp={st['bpp']}")
        print(f"  QP: {st['qp_raw_mean']} ({st['qp_scale']}), ekw. {st['qp_eq_mean']}  -> {s['qp']}")
        print(f"  bitrate/złożoność: SI={st['si_mean']} TI={st['ti_mean']}  -> {s['bits']}")
        print(f"  CAMBI: śr={st['cambi_mean']} max={st['cambi_max']}  -> {s['cambi']}")
        for wmsg in r["warnings"]:
            print("  ! " + wmsg)
        if a.json:
            with open(a.json, "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=1)
            a.json = None


if __name__ == "__main__":
    cli(sys.argv[1:])
