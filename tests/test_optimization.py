import copy
import gc
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evaluator_core as core
import performance as perf


class StorageTests(unittest.TestCase):
    def test_atomic_replacement_survives_invalid_result(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'result.json'
            perf.atomic_json(path, {'score': 71})
            with self.assertRaises(ValueError):
                perf.atomic_json(path, {'score': float('nan')})
            self.assertEqual(perf.read_json(path), {'score': 71})
            self.assertEqual(list(Path(td).glob('*.tmp')), [])

    def test_cache_invalidates_source_settings_and_binaries(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td)/'video.mp4'; source.write_bytes(b'source')
            ff = Path(td)/'ff.exe'; ff.write_bytes(b'tool')
            tools = type('Tools', (), {'ffmpeg':str(ff),'ffprobe':str(ff)})()
            key = perf.result_key(source, {'mode':'balanced'}, tools)
            self.assertNotEqual(key, perf.result_key(source, {'mode':'accurate'}, tools))
            source.write_bytes(b'changed source')
            self.assertNotEqual(key, perf.result_key(source, {'mode':'balanced'}, tools))
            source.write_bytes(b'source'); key = perf.result_key(source, {}, tools)
            ff.write_bytes(b'changed binary')
            self.assertNotEqual(key, perf.result_key(source, {}, tools))


class EngineTests(unittest.TestCase):
    def test_accuracy_mode_respects_explicit_sampling(self):
        self.assertEqual(core.Analyzer(None, mode='accurate').cambi_subsample, 1)
        self.assertEqual(core.Analyzer(None, mode='accurate', cambi_subsample=5).cambi_subsample, 5)
        with self.assertRaises(ValueError): core.Analyzer(None, mode='unknown')

    def test_parallel_progress_tracks_both_passes(self):
        events = []
        a = core.Analyzer(None, progress=lambda p,f,m:events.append(f))
        a._report_parallel_progress('pixels', 1, '')
        a._report_parallel_progress('bitstream', .5, '')
        a._report_parallel_progress('bitstream', .1, '')
        self.assertEqual(events, sorted(events))
        self.assertLess(events[0], 1)
        self.assertEqual(events[-2], events[-1])

    def test_cancel_terminates_registered_children(self):
        import subprocess
        a = core.Analyzer(None)
        child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],
                                 creationflags=core.NO_WINDOW)
        try:
            a._register_proc(child)
            a.cancel()
            child.wait(timeout=5)
            with self.assertRaises(core.Cancelled): a._check()
        finally:
            if child.poll() is None: child.kill()
            child.wait()

    def test_cached_result_launches_no_video_process(self):
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/'video.mp4'; source.write_bytes(b'video')
            ff=Path(td)/'ff.exe'; ff.write_bytes(b'tool')
            tools=type('Tools',(),{'ok':True,'ffmpeg':str(ff),'ffprobe':str(ff)})()
            a=core.Analyzer(tools)
            key=perf.result_key(source,[a.sc,a.use_cambi,a.cambi_subsample,a.mode],tools)
            perf.atomic_json(Path(td)/('result-'+key+'.json'),
                             {'frames':[], 'scores':{}, 'stats':{}, 'info':{}})
            with patch.object(perf,'data_dir',return_value=Path(td)), patch.object(core.subprocess,'Popen',side_effect=AssertionError('FFmpeg launched')):
                self.assertTrue(a.analyze(source)['cache_hit'])

    def test_scoring_is_unchanged_from_reference(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'work'))
        import baseline_core
        baseline=Path(__file__).resolve().parents[1]/'work'/'baseline_20260720-1500.MP4.json'
        if not baseline.exists(): self.skipTest('Run reference benchmark first')
        r=json.loads(baseline.read_text(encoding='utf-8'))
        args=[r[k] for k in ('info','frames','siti','cambi')]
        self.assertEqual(core.Analyzer(None).score(*args),baseline_core.Analyzer(None).score(*args))

    def test_chart_keeps_spikes_and_raw_data(self):
        import tkinter as tk
        import sportcam_evaluator as gui
        root=tk.Tk(); root.withdraw()
        try:
            chart=gui.Chart(root)
            chart.winfo_ismapped=lambda:True
            xs=list(range(100000)); ys=[25.0]*100000; ys[50001]=51; ys[50002]=0
            chart.set([{'kind':'points','xs':xs,'ys':ys,'color':'red'}],100000)
            until=time.monotonic()+10
            while not hasattr(chart,'_render_series') and time.monotonic()<until:
                root.update();time.sleep(.01)
            rendered=chart._render_series[0]
            self.assertIn(51,rendered['ys']);self.assertIn(0,rendered['ys'])
            self.assertEqual(len(chart.series[0]['xs']),100000)
            self.assertLess(len(chart.find_all()),500)
        finally:
            root.destroy()
            del chart
            del root
            gc.collect()

    @unittest.skipUnless(os.environ.get('SPORTCAM_TEST_VIDEO'), 'Set SPORTCAM_TEST_VIDEO for real FFmpeg integration')
    def test_gpu_failure_retries_only_failed_excerpt_on_cpu(self):
        path=os.environ['SPORTCAM_TEST_VIDEO']
        a=core.Analyzer(core.Tools(),cache=False)
        info=a.probe(path)
        a._verify_cambi_only()
        a.tools.hwaccel='not_a_real_backend'
        s,c=a._run_pixel_segment(path,info,0,0,.3,True,True,1,2,True,lambda *a:None)
        self.assertTrue(s)
        self.assertTrue(c)
        self.assertEqual(a.execution[-1]['decoder'],'cpu')
        self.assertFalse(a._procs)
        a.execution.clear()
        # Four failed GPU attempts must finish before CPU retries start.
        s,c=a.continuous_segments(path,info,4,1,2,duration=.3)
        self.assertEqual(len(s), round(.3*info['fps']))
        self.assertTrue(c)
        self.assertEqual({r['decoder'] for r in a.execution},{'cpu'})
        self.assertFalse(a._gpu_pending)
        self.assertFalse(a._procs)

    def test_corrupt_source_cannot_create_cached_success(self):
        tools=core.Tools()
        if not tools.ok: self.skipTest('FFmpeg unavailable')
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'invalid.bin';path.write_bytes(b'not a video')
            with patch.object(perf,'data_dir',return_value=Path(td)):
                with self.assertRaises(core.AnalysisError):
                    core.Analyzer(tools).analyze(path)
            self.assertEqual(list(Path(td).glob('result-*.json')),[])


if __name__=='__main__': unittest.main(verbosity=2)
