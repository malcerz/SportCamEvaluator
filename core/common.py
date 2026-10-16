"""
SportCamEvaluator - silnik analizy (GPU DirectX + Multi-Segment CPU).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys

APP_NAME = "SportCamEvaluator"
APP_VERSION = "1.3.0"

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
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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



def _frac(s):
    try:
        if "/" in s:
            a, b = s.split("/")
            return float(a) / float(b) if float(b) else 0.0
        return float(s)
    except Exception:
        return 0.0


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

RE_PACKET = re.compile(
    r"Packet: (\d+) bytes(, key frame)?.*?, pts (-?\d+|NOPTS|N/A)"
)

RE_FIELD = re.compile(
    r"\]\s+\d+\s+([A-Za-z_][\w\[\]]*)\s+\S*\s*=\s*(-?\d+)\s*$"
)

SUPPORTED_QP = {"h264", "hevc", "av1", "vp9"}


def _r(x, n=1):
    return None if x is None else round(x, n)
