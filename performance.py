"""Small, dependency-free persistent analysis storage."""
from __future__ import annotations
import hashlib
import json
import os
import platform
import tempfile
import threading
from pathlib import Path

ENGINE_REVISION = '10-pts-exact-segment-ranges'

def data_dir():
    root = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share'))
    path = root / 'SportCamEvaluator' / 'cache-v2'
    path.mkdir(parents=True, exist_ok=True)
    return path

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()

def atomic_json(path, value):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None

def result_key(path, settings, tools):
    st = os.stat(path)
    # Binary changes invalidate cached metric results without launching FFmpeg.
    binaries = [(p, os.stat(p).st_size, os.stat(p).st_mtime_ns)
                for p in (tools.ffmpeg, tools.ffprobe) if p]
    return digest([os.path.normcase(os.path.realpath(path)), st.st_size,
                   st.st_mtime_ns, ENGINE_REVISION, settings, binaries])

def clear_cache():
    count = 0
    for p in data_dir().glob('result-*.json'):
        try:
            p.unlink()
            count += 1
        except OSError:
            pass
    return count

def machine_key():
    return [platform.machine(), platform.processor(), os.cpu_count(),
            os.environ.get('PROCESSOR_IDENTIFIER', '')]

def available_memory():
    if os.name != 'nt':
        return None
    import ctypes as ct
    class Status(ct.Structure):
        _fields_ = [('length', ct.c_ulong), ('load', ct.c_ulong)] + [
            (n, ct.c_ulonglong) for n in ('total', 'available', 'total_page',
                'available_page', 'total_virtual', 'available_virtual', 'extended')]
    status = Status()
    status.length = ct.sizeof(status)
    if ct.windll.kernel32.GlobalMemoryStatusEx(ct.byref(status)):
        return status.available
    return None

class PeakMemory:
    """Sample concurrent working sets, not the sum of lifetime process peaks."""
    def __init__(self, process_ids):
        self.process_ids = process_ids
        self.peak = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        if os.name != 'nt':
            return
        import ctypes as ct
        from ctypes import wintypes as wt
        class Counters(ct.Structure):
            _fields_ = [('cb', wt.DWORD), ('faults', wt.DWORD)] + [
                (n, ct.c_size_t) for n in ('peak_ws', 'ws', 'peak_paged', 'paged',
                    'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]
        kernel, psapi = ct.WinDLL('kernel32'), ct.WinDLL('psapi')
        kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel.OpenProcess.restype = wt.HANDLE
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ct.POINTER(Counters), wt.DWORD]
        while not self.stop_event.is_set():
            total, valid = 0, False
            for pid in self.process_ids():
                handle = kernel.OpenProcess(0x410, False, pid)
                if not handle:
                    continue
                try:
                    counter = Counters()
                    counter.cb = ct.sizeof(counter)
                    if psapi.GetProcessMemoryInfo(handle, ct.byref(counter), counter.cb):
                        total += counter.ws
                        valid = True
                finally:
                    kernel.CloseHandle(handle)
            if valid:
                self.peak = max(self.peak or 0, total)
            self.stop_event.wait(.1)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)
        return self.peak
