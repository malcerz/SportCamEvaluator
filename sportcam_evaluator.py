"""Backward-compatible entry point for the SportCamEvaluator GUI."""
from __future__ import annotations

import os
import sys

from gui.charts import Chart, ChartSpecs, PlotSpec, prepare_chart_data
from gui.widgets import Gauge, ScoreBar, HelpWindow, score_color, fmt, human_size, human_time
from gui.main_window import App, config_file, load_config, save_config, main

if __name__ == "__main__":
    main()
