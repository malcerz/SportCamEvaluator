"""Backward-compatible entry point for the SportCamEvaluator core."""
from __future__ import annotations

import json
import subprocess
import sys

import performance as perf
from core.common import (
    APP_NAME, APP_VERSION, CPU_COUNT, DEFAULT_SCORING, GRADES, IS_WIN,
    NO_WINDOW, RE_FIELD, RE_PACKET, SUPPORTED_QP, AnalysisError, Cancelled,
    _frac, _r, _run, app_dir, find_tool, grade_for, interp, load_scoring,
    mean, percentile,
)
from core.hardware import Tools
from core.analyzer import Analyzer
from core.export import export_csv
from core.cli import cli

__all__ = [
    "APP_NAME", "APP_VERSION", "Analyzer", "AnalysisError", "CPU_COUNT",
    "Cancelled", "DEFAULT_SCORING", "GRADES", "IS_WIN", "NO_WINDOW",
    "RE_FIELD", "RE_PACKET", "SUPPORTED_QP", "Tools", "_frac", "_r",
    "_run", "app_dir", "cli", "export_csv", "find_tool", "grade_for",
    "interp", "load_scoring", "mean", "percentile",
]

if __name__ == "__main__":
    cli(sys.argv[1:])
