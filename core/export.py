from __future__ import annotations

from .common import _r

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
