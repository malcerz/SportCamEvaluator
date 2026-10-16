from __future__ import annotations

import math

from .common import AnalysisError, interp, mean, percentile, grade_for, _r

class ScoringMixin:
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
