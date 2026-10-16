"""Repeatable comparisons on existing compressed files; never writes video."""
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path
import time

import evaluator_core as core


def load_reference(path):
    spec = importlib.util.spec_from_file_location('sportcam_reference', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+')
    parser.add_argument('--reference', type=Path, help='Unmodified evaluator_core.py')
    parser.add_argument('--output', type=Path, default=Path('Raporty')/'benchmark.json')
    parser.add_argument('--skip-calibration', action='store_true')
    parser.add_argument('--diagnostic-seconds', type=float, default=3)
    args = parser.parse_args()
    tools = core.Tools()
    reference = load_reference(args.reference) if args.reference else None
    report = {'ffmpeg': tools.version, 'files': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        analyzer = core.Analyzer(tools, force=True)
        row = {'file': str(Path(path).resolve())}
        if reference:
            old_tools = reference.Tools(str(Path(tools.ffmpeg).parent))
            old = reference.Analyzer(old_tools, scoring=core.load_scoring())
            info = analyzer.probe(path)
            analyzer._verify_cambi_only()
            diagnostics = []
            for name, si, ca in [('siti', True, False), ('cambi', False, True), ('combined', True, True)]:
                result = {}
                values = []
                for label, runner in [('old', old), ('new', analyzer)]:
                    start = time.perf_counter()
                    values.append(runner._run_pixel_segment(path, info, 0, 0,
                        args.diagnostic_seconds, si, ca,
                        max(1, round(info['nb_frames']/400)), 8, False, lambda *a: None))
                    result[label+'_seconds'] = time.perf_counter()-start
                result.update(metric=name, equivalent=values[0] == values[1], backend='cpu')
                diagnostics.append(result)
                if not result['equivalent']:
                    raise RuntimeError('Metric regression: '+name+' on '+path)
            row['diagnostics'] = diagnostics
            start = time.perf_counter()
            original = old.analyze(path)
            row['old_seconds'] = time.perf_counter()-start
            row['old_stats'], row['old_scores'] = original['stats'], original['scores']
        if not args.skip_calibration:
            row['calibration'] = analyzer.benchmark(path)
        try:
            current = analyzer.analyze(path)
        except KeyboardInterrupt:
            analyzer.cancel()
            raise
        row['new_stats'], row['new_scores'] = current['stats'], current['scores']
        row['timings'], row['execution'] = current['timings'], current['execution']
        if reference:
            row['headers_equal'] = original['frames'] == current['frames']
            row['full_metrics_equal'] = {k:original[k] == current[k]
                                          for k in ('siti', 'cambi', 'windows', 'scores')}
            # Different old segment boundaries are reported, never hidden by tolerances.
        cached = core.Analyzer(tools).analyze(path)
        row['cache_load_seconds'] = cached.get('cache_load_seconds')
        row['cache_hit'] = cached['cache_hit']
        report['files'].append(row)
        core.perf.atomic_json(args.output, report)
        print(json.dumps(row, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    run()
