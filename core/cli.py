from __future__ import annotations

import json
import performance as perf
from .common import APP_NAME, AnalysisError
from .hardware import Tools
from .analyzer import Analyzer

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
