"""
SportCamEvaluator - GUI.

Uruchomienie:  python sportcam_evaluator.py  [pliki...]
Tryb wsadowy:  python sportcam_evaluator.py --cli plik1.mp4 plik2.mp4
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import evaluator_core as core

# ----------------------------------------------------------------------------
# Wygląd
# ----------------------------------------------------------------------------
C = {
    "bg": "#16181d",
    "panel": "#1f222a",
    "panel2": "#262a34",
    "line": "#343946",
    "fg": "#e8eaf0",
    "muted": "#8e95a6",
    "accent": "#4da3ff",
    "I": "#ff6b6b",
    "P": "#4da3ff",
    "B": "#51cf66",
    "qp": "#ffa94d",
    "cambi": "#cc5de8",
    "score": "#51cf66",
    "si": "#74c0fc",
    "ti": "#ffd43b",
}
FONT = "Segoe UI" if core.IS_WIN else "DejaVu Sans"
MONO = "Consolas" if core.IS_WIN else "DejaVu Sans Mono"


def score_color(s):
    if s is None:
        return C["muted"]
    s = max(0.0, min(100.0, s))
    # czerwony -> żółty -> zielony
    if s < 50:
        t = s / 50
        r, g, b = 255, int(80 + 132 * t), 70
    else:
        t = (s - 50) / 50
        r, g, b = int(255 - 174 * t), int(212 - 5 * t), int(70 + 32 * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def fmt(v, n=1, suffix=""):
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.{n}f}{suffix}"
    return f"{v}{suffix}"


def human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def human_time(s):
    s = int(round(s or 0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ----------------------------------------------------------------------------
# Konfiguracja użytkownika
# ----------------------------------------------------------------------------
def config_file():
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    d = os.path.join(base, core.APP_NAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "config.json")


def load_config():
    try:
        with open(config_file(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(config_file(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Wykres na Canvas (bez zewnętrznych bibliotek)
# ----------------------------------------------------------------------------
class Chart(tk.Canvas):
    PAD_L, PAD_R, PAD_T, PAD_B = 56, 16, 28, 30

    def __init__(self, master, title="", y_label="", **kw):
        super().__init__(master, bg=C["panel"], highlightthickness=0, **kw)
        self.title = title
        self.y_label = y_label
        self.series = []        # dict(kind, xs, ys, colors|color, name)
        self.y_range = None
        self.bands = []         # (y0, y1, color)
        self.x_max = 1.0
        self.marker_x = None
        self.empty_msg = "Brak danych"
        self._redraw_job = None
        self._limits = None
        self.bind("<Configure>", lambda e: self.schedule_redraw())
        self.bind("<Map>", lambda e: self.schedule_redraw())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self.delete("hover"))

    def set(self, series, x_max, y_range=None, bands=None, empty_msg=None, marker_x=None):
        self.series = series
        self.x_max = max(x_max, 1e-6)
        self.y_range = y_range
        self.bands = bands or []
        self.marker_x = marker_x
        if empty_msg:
            self.empty_msg = empty_msg
        self._limits = None
        self.schedule_redraw()

    def schedule_redraw(self):
        if self._redraw_job is not None:
            self.after_cancel(self._redraw_job)
        self._redraw_job = self.after(80, self._redraw_visible)

    def _redraw_visible(self):
        self._redraw_job = None
        if not self.winfo_ismapped():
            return
        width = max(1, int(self._geom()[2]-self._geom()[0]))
        previous = getattr(self, '_render_token', None)
        if previous is not None:
            previous.set()
        token = threading.Event()
        self._render_token = token
        state = {}
        series, x_max = self.series, self.x_max
        def prepare():
            rendered, low, high = [], None, None
            for source in series:
                columns = {}
                for i,(x,y) in enumerate(zip(source['xs'], source['ys'])):
                    if i % 8192 == 0 and token.is_set():
                        return
                    if y is None:
                        continue
                    low = y if low is None else min(low,y)
                    high = y if high is None else max(high,y)
                    px = min(width-1,max(0,int(x/x_max*width)))
                    pair = columns.setdefault(px,[i,i])
                    if y < source['ys'][pair[0]]: pair[0] = i
                    if y > source['ys'][pair[1]]: pair[1] = i
                indices = sorted({i for pair in columns.values() for i in pair})
                item = dict(source)
                for key in ('xs','ys','colors','types'):
                    if key in source:
                        item[key] = [source[key][i] for i in indices]
                rendered.append(item)
            state['result'] = (rendered, low, high)
        self._render_thread = threading.Thread(target=prepare,daemon=True)
        self._render_thread.start()
        self._finish_render(token,state,series)

    def _finish_render(self, token, state, series):
        if self._render_token is not token or token.is_set():
            return
        if 'result' not in state:
            self._redraw_job = self.after(20,self._finish_render,token,state,series)
            return
        self._redraw_job = None
        self._render_series, low, high = state['result']
        if self.y_range:
            self._limits = self.y_range
        else:
            low,high = (0,1) if low is None else (low,high)
            if s_kind_bars(series): low = min(low,0)
            if high-low < 1e-9: high = low+1
            pad = (high-low)*0.08
            self._limits = (low-(0 if s_kind_bars(series) else pad),high+pad)
        start = time.perf_counter()
        self.redraw()
        self.last_draw_seconds = time.perf_counter()-start
        if getattr(self, 'on_draw', None):
            self.on_draw(self.last_draw_seconds)

    def _geom(self):
        w, h = self.winfo_width(), self.winfo_height()
        return self.PAD_L, self.PAD_T, max(w - self.PAD_R, self.PAD_L + 10), max(h - self.PAD_B, self.PAD_T + 10)

    def _yr(self):
        if self._limits is not None:
            return self._limits
        if self.y_range:
            return self.y_range
        ys = [y for s in self.series for y in s["ys"] if y is not None]
        if not ys:
            return (0, 1)
        lo, hi = min(ys), max(ys)
        lo = min(lo, 0) if s_kind_bars(self.series) else lo
        if hi - lo < 1e-9:
            hi = lo + 1
        pad = (hi - lo) * 0.08
        return (lo - (0 if s_kind_bars(self.series) else pad), hi + pad)

    def redraw(self):
        self.delete("all")
        x0, y0, x1, y1 = self._geom()
        self.create_text(x0, 8, text=self.title, anchor="nw", fill=C["fg"], font=(FONT, 10, "bold"))
        legend_x = x1
        for s in reversed(self.series):
            if s.get("legend"):
                for name, col in reversed(s["legend"]):
                    t = self.create_text(legend_x, 10, text=name, anchor="ne", fill=C["muted"], font=(FONT, 9))
                    bx = self.bbox(t)[0]
                    self.create_rectangle(bx - 14, 6, bx - 5, 15, fill=col, outline="")
                    legend_x = bx - 22
        if not any(s["xs"] for s in self.series):
            self.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=self.empty_msg, fill=C["muted"], font=(FONT, 10))
            return
        lo, hi = self._yr()
        self._lo, self._hi = lo, hi
        sx = (x1 - x0) / self.x_max
        sy = (y1 - y0) / (hi - lo)
        Y = lambda v: y1 - (v - lo) * sy
        for b0, b1, col in self.bands:
            self.create_rectangle(x0, Y(min(b1, hi)), x1, Y(max(b0, lo)), fill=col, outline="")
        # siatka
        for i in range(5):
            v = lo + (hi - lo) * i / 4
            yy = Y(v)
            self.create_line(x0, yy, x1, yy, fill=C["line"])
            self.create_text(x0 - 6, yy, text=_nice(v), anchor="e", fill=C["muted"], font=(FONT, 8))
        step = _time_step(self.x_max)
        t = 0.0
        while t <= self.x_max + 1e-9:
            xx = x0 + t * sx
            self.create_line(xx, y0, xx, y1, fill=C["line"])
            self.create_text(xx, y1 + 4, text=_tlabel(t, step), anchor="n", fill=C["muted"], font=(FONT, 8))
            t += step
        if self.y_label:
            self.create_text(6, (y0 + y1) / 2, text=self.y_label, angle=90, fill=C["muted"], font=(FONT, 8))
        self.create_rectangle(x0, y0, x1, y1, outline=C["line"])

        width_px = max(int(x1 - x0), 1)
        for s in getattr(self, "_render_series", self.series):
            xs, ys = s["xs"], s["ys"]
            if s["kind"] == "bars":
                # agregacja do kolumn pikseli: max wysokości, kolor wg priorytetu typu
                cols = {}
                for x, y, c in zip(xs, ys, s["colors"]):
                    if y is None:
                        continue
                    px = min(int(x * sx), width_px - 1)
                    cur = cols.get(px)
                    if cur is None or y > cur[0]:
                        cols[px] = (y, c)
                bw = max(1.0, (x1 - x0) / max(len(xs), 1) * 0.9)
                for px, (y, c) in cols.items():
                    xx = x0 + px
                    self.create_rectangle(xx, Y(y), xx + bw, Y(max(lo, 0)), fill=c, outline="")
            elif s["kind"] == "line":
                pts = []
                buckets = {}
                for x, y in zip(xs, ys):
                    if y is None:
                        continue
                    px = int(x * sx)
                    b = buckets.setdefault(px, [y, y])
                    b[0] = min(b[0], y)
                    b[1] = max(b[1], y)
                for px in sorted(buckets):
                    low, high = buckets[px]
                    pts += [x0 + px, Y(low), x0 + px, Y(high)]
                if len(pts) >= 4:
                    self.create_line(*pts, fill=s["color"], width=s.get("width", 2), smooth=False)
                elif len(pts) == 2:
                    self.create_oval(pts[0] - 3, pts[1] - 3, pts[0] + 3, pts[1] + 3, fill=s["color"], outline="")
            elif s["kind"] == "points":
                buckets = {}
                for x, y in zip(xs, ys):
                    if y is None:
                        continue
                    px = min(max(0, int(x*sx)), max(0, int(x1-x0)-1))
                    b = buckets.setdefault(px, [y, y])
                    b[0], b[1] = min(b[0], y), max(b[1], y)
                for px, (low, high) in buckets.items():
                    xx = x0 + px
                    if low == high:
                        yy = Y(low)
                        self.create_oval(xx-2, yy-2, xx+2, yy+2, fill=s["color"], outline="")
                    else:
                        self.create_line(xx, Y(low), xx, Y(high), fill=s["color"], width=2)
        if self.marker_x is not None:
            xx = x0 + self.marker_x * sx
            self.create_line(xx, y0, xx, y1, fill=C["I"], dash=(3, 3))

    def _on_motion(self, e):
        self.delete("hover")
        if not self.series or not any(s["xs"] for s in self.series):
            return
        x0, y0, x1, y1 = self._geom()
        if not (x0 <= e.x <= x1 and y0 <= e.y <= y1):
            return
        t = (e.x - x0) / (x1 - x0) * self.x_max
        lines = [human_time(t) + f" ({t:.2f} s)"]
        for s in self.series:
            xs = s["xs"]
            if not xs:
                continue
            i = _nearest(xs, t)
            y = s["ys"][i]
            if y is None:
                continue
            nm = s.get("name", "")
            extra = f" [{s['types'][i]}]" if s.get("types") else ""
            lines.append(f"{nm}: {_nice(y)}{extra}")
        self.create_line(e.x, y0, e.x, y1, fill=C["muted"], tags="hover")
        txt = "\n".join(lines)
        tx = e.x + 10 if e.x < (x0 + x1) / 2 else e.x - 10
        anchor = "nw" if e.x < (x0 + x1) / 2 else "ne"
        tid = self.create_text(tx, y0 + 6, text=txt, anchor=anchor, fill=C["fg"], font=(FONT, 9), tags="hover")
        bb = self.bbox(tid)
        r = self.create_rectangle(bb[0] - 6, bb[1] - 4, bb[2] + 6, bb[3] + 4, fill=C["panel2"],
                                  outline=C["line"], tags="hover")
        self.tag_lower(r, tid)


def s_kind_bars(series):
    return any(s["kind"] == "bars" for s in series)


def _nearest(xs, t):
    lo, hi = 0, len(xs) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if xs[mid] < t:
            lo = mid + 1
        else:
            hi = mid
    if lo > 0 and abs(xs[lo - 1] - t) < abs(xs[lo] - t):
        return lo - 1
    return lo


def _nice(v):
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e4:
        return f"{v / 1e3:.0f}k"
    if a >= 100:
        return f"{v:.0f}"
    if a >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def _tlabel(t, step):
    if step < 1:
        return f"{t:.1f} s"
    if step < 60 and t < 60:
        return f"{t:.0f} s"
    return human_time(t)


def _time_step(dur):
    for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
        if dur / s <= 10:
            return s
    return 7200


# ----------------------------------------------------------------------------
# Wskaźnik oceny
# ----------------------------------------------------------------------------
class Gauge(tk.Canvas):
    def __init__(self, master, size=150, **kw):
        super().__init__(master, width=size, height=size, bg=C["panel"], highlightthickness=0, **kw)
        self.size = size
        self.set(None, "")

    def set(self, score, grade):
        self.delete("all")
        s, p = self.size, 12
        self.create_arc(p, p, s - p, s - p, start=225, extent=-270, style="arc",
                        outline=C["line"], width=12)
        if score is not None:
            self.create_arc(p, p, s - p, s - p, start=225, extent=-270 * score / 100, style="arc",
                            outline=score_color(score), width=12)
            self.create_text(s / 2, s / 2 - 6, text=f"{score:.0f}", fill=C["fg"], font=(FONT, 34, "bold"))
            self.create_text(s / 2, s / 2 + 28, text=grade, fill=score_color(score), font=(FONT, 10, "bold"))
        else:
            self.create_text(s / 2, s / 2, text="–", fill=C["muted"], font=(FONT, 34, "bold"))


class ScoreBar(tk.Frame):
    def __init__(self, master, label, hint):
        super().__init__(master, bg=C["panel"])
        top = tk.Frame(self, bg=C["panel"])
        top.pack(fill="x")
        tk.Label(top, text=label, bg=C["panel"], fg=C["fg"], font=(FONT, 10)).pack(side="left")
        self.val = tk.Label(top, text="–", bg=C["panel"], fg=C["fg"], font=(FONT, 10, "bold"))
        self.val.pack(side="right")
        self.bar = tk.Canvas(self, height=8, bg=C["panel2"], highlightthickness=0)
        self.bar.pack(fill="x", pady=(3, 0))
        self.hint = tk.Label(self, text=hint, bg=C["panel"], fg=C["muted"], font=(FONT, 8), anchor="w",
                             justify="left", wraplength=310)
        self.hint.pack(fill="x")
        self._v = None
        self.bar.bind("<Configure>", lambda e: self._draw())

    def set(self, v, hint=None):
        self._v = v
        self.val.config(text=fmt(v, 0), fg=score_color(v) if v is not None else C["muted"])
        if hint is not None:
            self.hint.config(text=hint)
        self._draw()

    def _draw(self):
        self.bar.delete("all")
        if self._v is None:
            return
        w = self.bar.winfo_width()
        self.bar.create_rectangle(0, 0, w * self._v / 100, 8, fill=score_color(self._v), outline="")


# ----------------------------------------------------------------------------
# Główne okno
# ----------------------------------------------------------------------------
VIDEO_TYPES = [("Pliki wideo", "*.mp4 *.mov *.mkv *.m4v *.webm *.ts *.mts *.m2ts *.avi *.insv *.lrv *.360"),
               ("Wszystkie pliki", "*.*")]


class PlotSpec:
    def set(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs

class ChartSpecs:
    def __init__(self):
        for name in ('ch_score', 'ch_qp', 'ch_size', 'ch_cambi', 'ch_siti'):
            setattr(self, name, PlotSpec())

def prepare_chart_data(res):
    self = ChartSpecs()
    fr, win, cam, siti = res["frames"], res["windows"], res["cambi"], res["siti"]
    dur = max([f["t"] for f in fr] + [w["t"] + 1 for w in win] + [1.0])
    worst = res["scores"]["worst_window_t"]
    bands = [(0, 40, "#2a1f24"), (40, 60, "#2a2720"), (60, 75, "#23271f"), (75, 100, "#1e2a24")]
    self.ch_score.set([
        {"kind": "line", "xs": [w["t"] + 0.5 for w in win], "ys": [w["score"] for w in win],
         "color": C["score"], "name": "ocena", "width": 2,
         "legend": [("ocena", C["score"]), ("QP", C["qp"]), ("bitrate", C["si"]), ("CAMBI", C["cambi"])]},
        {"kind": "line", "xs": [w["t"] + 0.5 for w in win], "ys": [w["s_qp"] for w in win],
         "color": C["qp"], "name": "ocena QP", "width": 1},
        {"kind": "line", "xs": [w["t"] + 0.5 for w in win], "ys": [w["s_bits"] for w in win],
         "color": C["si"], "name": "ocena bitrate", "width": 1},
        {"kind": "line", "xs": [w["t"] + 0.5 for w in win], "ys": [w["s_cambi"] for w in win],
         "color": C["cambi"], "name": "ocena CAMBI", "width": 1},
    ], dur, y_range=(0, 100), bands=bands, marker_x=worst + 0.5)

    qcol = [C[f["type"]] for f in fr]
    legend = [("I", C["I"]), ("P", C["P"]), ("B", C["B"])]
    self.ch_qp.title = f"QP każdej ramki ({res['stats']['qp_scale']}, wartości surowe)"
    self.ch_qp.set([
        {"kind": "points", "xs": [f["t"] for f in fr if f["type"] == tp],
         "ys": [f["qp_raw"] for f in fr if f["type"] == tp], "color": C[tp], "name": f"QP {tp}"}
        for tp in ("B", "P", "I")
    ] + [{"kind": "line", "xs": [], "ys": [], "color": C["fg"], "legend": legend}],
        dur, empty_msg="Brak QP dla tego kodeka")
    self.ch_size.set([{"kind": "bars", "xs": [f["t"] for f in fr], "ys": [f["size"] for f in fr],
                       "colors": qcol, "types": [f["type"] for f in fr], "name": "rozmiar [B]",
                       "legend": legend}], dur)
    self.ch_cambi.set([{"kind": "line", "xs": [c["t"] for c in cam], "ys": [c["cambi"] for c in cam],
                        "color": C["cambi"], "name": "CAMBI"}], dur,
                      y_range=(0, max(10.0, max([c["cambi"] for c in cam] + [0]) * 1.1)),
                      bands=[(5, 100, "#2a1f2a")],
                      empty_msg="CAMBI nie liczone (wyłączone lub brak libvmaf w ffmpeg)")
    self.ch_siti.set([
        {"kind": "line", "xs": [s["t"] for s in siti], "ys": [s["si"] for s in siti], "color": C["si"],
         "name": "SI", "width": 1, "legend": [("SI", C["si"]), ("TI", C["ti"])]},
        {"kind": "line", "xs": [s["t"] for s in siti], "ys": [s["ti"] for s in siti], "color": C["ti"],
         "name": "TI", "width": 1},
    ], dur, empty_msg="Brak SI/TI")
    return self


class App(tk.Tk):
    def __init__(self, files=None):
        super().__init__()
        self.title(f"{core.APP_NAME} {core.APP_VERSION}")
        self.geometry("1360x900")
        self.minsize(1000, 680)
        self.configure(bg=C["bg"])
        self.cfg = load_config()
        self.tools = core.Tools(self.cfg.get("ffmpeg_dir"))
        self.results = {}
        self.chart_specs = {}          # iid -> result
        self.queue = []            # [(iid, path)]
        self.msgs = queue.Queue()
        self.worker = None
        self.analyzer = None
        self.current_iid = None
        self.shown_iid = None

        self._style()
        self._menu()
        self._build()
        self._update_tools_label()
        self.after(100, self._poll)
        if not self.tools.ok:
            self.after(400, self._tools_missing)
        if files:
            self.add_files(files)

    # ------------------------------------------------------------------ style
    def _style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=C["bg"], foreground=C["fg"], fieldbackground=C["panel2"],
                     bordercolor=C["line"], lightcolor=C["line"], darkcolor=C["line"], font=(FONT, 10))
        st.configure("TButton", background=C["panel2"], foreground=C["fg"], padding=(12, 6), relief="flat")
        st.map("TButton", background=[("active", C["line"]), ("disabled", C["panel"])],
               foreground=[("disabled", C["muted"])])
        st.configure("Accent.TButton", background=C["accent"], foreground="#0b1220", font=(FONT, 10, "bold"))
        st.map("Accent.TButton", background=[("active", "#7cbcff"), ("disabled", C["panel2"])])
        st.configure("Treeview", background=C["panel"], fieldbackground=C["panel"], foreground=C["fg"],
                     rowheight=26, borderwidth=0)
        st.configure("Treeview.Heading", background=C["panel2"], foreground=C["muted"], relief="flat",
                     font=(FONT, 9, "bold"))
        st.map("Treeview", background=[("selected", "#2d4a6e")], foreground=[("selected", C["fg"])])
        st.map("Treeview.Heading", background=[("active", C["line"])])
        st.configure("TNotebook", background=C["bg"], borderwidth=0)
        st.configure("TNotebook.Tab", background=C["panel"], foreground=C["muted"], padding=(14, 6))
        st.map("TNotebook.Tab", background=[("selected", C["panel2"])], foreground=[("selected", C["fg"])])
        st.configure("TCheckbutton", background=C["bg"], foreground=C["fg"])
        st.map("TCheckbutton", background=[("active", C["bg"])])
        st.configure("TCombobox", foreground=C["fg"], background=C["panel2"], arrowcolor=C["fg"],
                     fieldbackground=C["panel2"], selectbackground=C["panel2"], selectforeground=C["fg"])
        st.map("TCombobox", fieldbackground=[("readonly", C["panel2"])], foreground=[("readonly", C["fg"])],
               selectbackground=[("readonly", C["panel2"])], selectforeground=[("readonly", C["fg"])])
        st.configure("Horizontal.TProgressbar", troughcolor=C["panel2"], background=C["accent"],
                     bordercolor=C["panel2"], lightcolor=C["accent"], darkcolor=C["accent"])
        st.configure("TPanedwindow", background=C["bg"])
        self.option_add("*TCombobox*Listbox.background", C["panel2"])
        self.option_add("*TCombobox*Listbox.foreground", C["fg"])

    def _menu(self):
        m = tk.Menu(self)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="Dodaj pliki do analizy…", command=self.choose_files, accelerator="Ctrl+O")
        f.add_separator()
        f.add_command(label="Eksportuj wynik do JSON…", command=lambda: self.export("json"))
        f.add_command(label="Eksportuj wynik do CSV…", command=lambda: self.export("csv"))
        f.add_command(label="Eksportuj tabelę porównawczą (CSV)…", command=self.export_table)
        f.add_separator()
        f.add_command(label="Usuń zaznaczone z listy", command=self.remove_selected)
        f.add_command(label="Zakończ", command=self.close)
        m.add_cascade(label="Plik", menu=f)
        s = tk.Menu(m, tearoff=0)
        s.add_command(label="Wskaż folder z ffmpeg…", command=self.choose_ffmpeg)
        s.add_command(label="Wykryj ffmpeg ponownie", command=self.redetect_tools)
        s.add_command(label="Wyczyść cache wyników", command=lambda: messagebox.showinfo(
            "Cache", f"Usunięto {core.perf.clear_cache()} zapisanych wyników."))
        s.add_command(label="Otwórz plik parametrów oceny (scoring.json)", command=self.open_scoring)
        m.add_cascade(label="Ustawienia", menu=s)
        h = tk.Menu(m, tearoff=0)
        h.add_command(label="Jak liczona jest ocena", command=self.show_help)
        h.add_command(label="O programie", command=self.show_about)
        m.add_cascade(label="Pomoc", menu=h)
        self.config(menu=m)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Control-o>", lambda e: self.choose_files())

    # ------------------------------------------------------------------ layout
    def _build(self):
        # pasek narzędzi
        bar = tk.Frame(self, bg=C["bg"])
        bar.pack(fill="x", padx=14, pady=(12, 8))
        tk.Label(bar, text="SportCam", bg=C["bg"], fg=C["fg"], font=(FONT, 16, "bold")).pack(side="left")
        tk.Label(bar, text="Evaluator", bg=C["bg"], fg=C["accent"], font=(FONT, 16, "bold")).pack(side="left")
        tk.Label(bar, text="  ocena kompresji bez źródła", bg=C["bg"], fg=C["muted"],
                 font=(FONT, 10)).pack(side="left", pady=(5, 0))
        self.btn_stop = ttk.Button(bar, text="Zatrzymaj", command=self.stop, state="disabled")
        self.btn_stop.pack(side="right")
        self.btn_add = ttk.Button(bar, text="Dodaj pliki i analizuj…", style="Accent.TButton",
                                  command=self.choose_files)
        self.btn_add.pack(side="right", padx=(0, 8))
        opt = tk.Frame(self, bg=C["bg"])
        opt.pack(fill="x", padx=14, pady=(0, 8))
        self.var_cambi = tk.BooleanVar(value=self.cfg.get("use_cambi", True))
        ttk.Checkbutton(opt, text="CAMBI (banding)", variable=self.var_cambi,
                        command=self._save_opts).pack(side="left")
        tk.Label(opt, text="  co ile klatek:", bg=C["bg"], fg=C["muted"]).pack(side="left")
        self.var_sub = tk.StringVar(value=self.cfg.get("cambi_subsample", "Auto"))
        cb = ttk.Combobox(opt, textvariable=self.var_sub, width=6, state="readonly",
                          values=["Auto", "1", "2", "3", "5", "10", "20", "30"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._save_opts())

        self.var_mode = tk.StringVar(value=self.cfg.get("analysis_mode", "Zrównoważony"))
        mode_box = ttk.Combobox(opt, textvariable=self.var_mode, state="readonly", width=15,
                     values=["Szybki", "Zrównoważony", "Dokładny"])
        mode_box.pack(side="left", padx=10)
        mode_box.bind("<<ComboboxSelected>>", lambda e: self._save_opts())
        self.var_force = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Wymuś analizę", variable=self.var_force).pack(side="left")

        # status
        sb = tk.Frame(self, bg=C["panel"])
        sb.pack(fill="x", side="bottom")
        self.lbl_tools = tk.Label(sb, text="", bg=C["panel"], fg=C["muted"], font=(FONT, 9), anchor="w")
        self.lbl_tools.pack(side="left", padx=10, pady=4)
        self.progress = ttk.Progressbar(sb, length=260, mode="determinate", maximum=1000)
        self.progress.pack(side="right", padx=10, pady=4)
        self.lbl_status = tk.Label(sb, text="Gotowy.", bg=C["panel"], fg=C["fg"], font=(FONT, 9), anchor="e")
        self.lbl_status.pack(side="right", padx=4)

        pw = ttk.Panedwindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        # --- górna część: lista + podsumowanie
        top = tk.Frame(pw, bg=C["bg"])
        pw.add(top, weight=3)

        left = tk.Frame(top, bg=C["panel"])
        left.pack(side="left", fill="both", expand=True)
        cols = ("score", "grade", "codec", "res", "fps", "rate", "qp", "cambi", "status")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Plik", anchor="w")
        self.tree.column("#0", width=230, anchor="w")
        heads = {"score": ("Wynik", 60), "grade": ("Ocena", 95), "codec": ("Kodek", 60),
                 "res": ("Rozdz.", 90), "fps": ("FPS", 55), "rate": ("Mb/s", 60),
                 "qp": ("QP śr.", 60), "cambi": ("CAMBI", 60), "status": ("Status", 110)}
        for c, (t, w) in heads.items():
            self.tree.heading(c, text=t, command=lambda c=c: self._sort(c))
            self.tree.column(c, width=w, anchor="center", stretch=(c == "status"))
        ysb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())

        right = tk.Frame(top, bg=C["panel"], width=540)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)
        head = tk.Frame(right, bg=C["panel"])
        head.pack(fill="x", padx=14, pady=(12, 4))
        self.gauge = Gauge(head, size=150)
        self.gauge.pack(side="left")
        bars = tk.Frame(head, bg=C["panel"])
        bars.pack(side="left", fill="both", expand=True, padx=(14, 0))
        self.bar_qp = ScoreBar(bars, "Kwantyzacja (QP)", "główny wskaźnik strat")
        self.bar_qp.pack(fill="x", pady=(4, 6))
        self.bar_bits = ScoreBar(bars, "Bitrate vs złożoność", "bity na piksel / SI·TI")
        self.bar_bits.pack(fill="x", pady=6)
        self.bar_cambi = ScoreBar(bars, "Banding (CAMBI)", "kara za pasmowanie")
        self.bar_cambi.pack(fill="x", pady=6)
        self.lbl_name = tk.Label(right, text="Dodaj plik, aby rozpocząć.", bg=C["panel"], fg=C["fg"],
                                 font=(FONT, 10, "bold"), anchor="w", wraplength=510, justify="left")
        self.lbl_name.pack(fill="x", padx=14, pady=(6, 2))
        self.details = tk.Text(right, bg=C["panel"], fg=C["fg"], relief="flat", height=10, wrap="none",
                               font=(MONO, 9), highlightthickness=0, padx=14, pady=4)
        self.details.pack(fill="both", expand=True)
        self.details.tag_configure("k", foreground=C["muted"])
        self.details.tag_configure("w", foreground=C["qp"], wrap="word")
        self.details.config(state="disabled")

        # --- dolna część: wykresy
        nb = ttk.Notebook(pw)
        pw.add(nb, weight=3)
        self.ch_score = Chart(nb, "Ocena w czasie (okna 1 s)", "ocena")
        self.ch_qp = Chart(nb, "QP każdej ramki", "QP")
        self.ch_size = Chart(nb, "Rozmiar ramek", "bajty")
        self.ch_cambi = Chart(nb, "CAMBI – banding (0 = brak, >5 widoczny)", "CAMBI")
        self.ch_siti = Chart(nb, "Złożoność treści SI / TI (ITU-T P.910)", "SI / TI")
        nb.add(self.ch_score, text="Ocena")
        nb.add(self.ch_qp, text="QP")
        nb.add(self.ch_size, text="Rozmiar ramek")
        nb.add(self.ch_cambi, text="CAMBI")
        nb.add(self.ch_siti, text="SI / TI")
        self._clear_charts()

    def _clear_charts(self):
        for ch in (self.ch_score, self.ch_qp, self.ch_size, self.ch_cambi, self.ch_siti):
            ch.set([], 1, empty_msg="Brak danych – wybierz plik z listy")

    # --------------------------------------------------------------- narzędzia
    def _update_tools_label(self):
        p = self.tools.ffmpeg or "—"
        self.lbl_tools.config(text=f"{self.tools.describe()}   ({p})")

    def _tools_missing(self):
        messagebox.showwarning(
            core.APP_NAME,
            "Nie znaleziono ffmpeg/ffprobe.\n\n"
            "Uruchom get_ffmpeg.ps1 z folderu programu (pobiera pełną wersję z libvmaf), "
            "albo skopiuj ffmpeg.exe i ffprobe.exe do podfolderu 'ffmpeg', "
            "albo wskaż folder: Ustawienia → Wskaż folder z ffmpeg.")

    def choose_ffmpeg(self):
        d = filedialog.askdirectory(title="Folder z ffmpeg.exe i ffprobe.exe")
        if d:
            self.cfg["ffmpeg_dir"] = d
            save_config(self.cfg)
            self.redetect_tools()

    def redetect_tools(self):
        self.tools = core.Tools(self.cfg.get("ffmpeg_dir"))
        self._update_tools_label()
        if not self.tools.ok:
            self._tools_missing()

    def open_scoring(self):
        p = os.path.join(core.app_dir(), "scoring.json")
        if not os.path.isfile(p):
            with open(p, "w", encoding="utf-8") as f:
                json.dump(core.DEFAULT_SCORING, f, indent=2)
        try:
            if core.IS_WIN:
                os.startfile(p)  # type: ignore[attr-defined]
            else:
                messagebox.showinfo(core.APP_NAME, p)
        except Exception as e:
            messagebox.showerror(core.APP_NAME, str(e))

    def _save_opts(self):
        self.cfg["use_cambi"] = bool(self.var_cambi.get())
        self.cfg["cambi_subsample"] = self.var_sub.get()
        self.cfg["analysis_mode"] = self.var_mode.get()
        save_config(self.cfg)

    # ----------------------------------------------------------------- kolejka
    def choose_files(self):
        init = self.cfg.get("last_dir") or os.path.expanduser("~")
        files = filedialog.askopenfilenames(title="Wybierz pliki wideo", initialdir=init, filetypes=VIDEO_TYPES)
        if files:
            self.cfg["last_dir"] = os.path.dirname(files[0])
            save_config(self.cfg)
            self.add_files(files)

    def add_files(self, files):
        if not self.tools.ok:
            self._tools_missing()
            return
        first = None
        for p in files:
            iid = self.tree.insert("", "end", text=os.path.basename(p),
                                   values=("", "", "", "", "", "", "", "", "w kolejce"))
            self.queue.append((iid, p))
            first = first or iid
        self._start_next()

    def _start_next(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.queue:
            self.btn_stop.config(state="disabled")
            self.current_iid = None
            return
        iid, path = self.queue.pop(0)
        self.current_iid = iid
        self.tree.set(iid, "status", "analiza…")
        self.btn_stop.config(state="normal")
        sub = None if self.var_sub.get() == "Auto" else int(self.var_sub.get())

        def progress(phase, frac, msg, _iid=iid):
            # przebieg 1 (nagłówki) = 0-35 %, przebieg 2 (dekodowanie) = 35-100 %
            total = {"probe": 0.0, "bitstream": 0.35 * frac, "pixels": 0.35 + 0.65 * frac,
                     "score": 0.99, "done": 1.0, "analysis": frac, "decoder": None}.get(phase, 0)
            if total is None:
                self.msgs.put(("decoder", _iid, msg))
                return
            self.msgs.put(("progress", _iid, total, msg))

        self.analyzer = core.Analyzer(self.tools, core.load_scoring(), use_cambi=self.var_cambi.get(),
                                      cambi_subsample=sub, progress=progress,
                                      mode={"Szybki": "fast", "Zrównoważony": "balanced", "Dokładny": "accurate"}[self.var_mode.get()],
                                      force=self.var_force.get())
        an = self.analyzer

        def run():
            try:
                res = an.analyze(path)
                t = time.perf_counter()
                specs = prepare_chart_data(res)
                res.setdefault('timings', {})['gui_prepare'] = time.perf_counter()-t
                self.msgs.put(("done", iid, res, specs))
            except core.Cancelled:
                self.msgs.put(("cancelled", iid, None))
            except core.AnalysisError as e:
                self.msgs.put(("error", iid, str(e)))
            except Exception as e:  # nieoczekiwany błąd
                import traceback
                self.msgs.put(("error", iid, f"{e}\n\n{traceback.format_exc()[-1200:]}"))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def close(self):
        self.stop()
        for chart in (self.ch_score,self.ch_qp,self.ch_size,self.ch_cambi,self.ch_siti):
            token = getattr(chart,'_render_token',None)
            if token is not None:
                token.set()
        self._finish_close()

    def _finish_close(self):
        charts = (self.ch_score,self.ch_qp,self.ch_size,self.ch_cambi,self.ch_siti)
        drawing = any(getattr(c,'_render_thread',None) and c._render_thread.is_alive() for c in charts)
        if drawing or (self.worker and self.worker.is_alive()):
            self.after(50,self._finish_close)
            return
        self.analyzer = None
        self.worker = None
        self.destroy()

    def stop(self):
        for iid, _ in self.queue:
            self.tree.set(iid, "status", "anulowano")
        self.queue.clear()
        if self.analyzer:
            self.analyzer.cancel()

    def _poll(self):
        try:
            while True:
                m = self.msgs.get_nowait()
                kind, iid = m[0], m[1]
                if kind == "progress":
                    self.progress["value"] = m[2] * 1000
                    self.lbl_status.config(text=f"{self.tree.item(iid, 'text')} – {m[3]}")
                    self.tree.set(iid, "status", f"analiza {m[2] * 100:.0f}%")
                elif kind == "decoder":
                    self.lbl_status.config(text=m[2])
                elif kind == "done":
                    self.chart_specs[iid] = m[3]
                    self._on_done(iid, m[2])
                elif kind in ("error", "cancelled"):
                    self.tree.set(iid, "status", "błąd" if kind == "error" else "anulowano")
                    self.progress["value"] = 0
                    self.lbl_status.config(text="Gotowy.")
                    if kind == "error":
                        messagebox.showerror(core.APP_NAME, f"{self.tree.item(iid, 'text')}\n\n{m[2]}")
                    self.worker = None
                    self._start_next()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _on_done(self, iid, res):
        self.results[iid] = res
        s, st, inf = res["scores"], res["stats"], res["info"]
        self.tree.item(iid, values=(
            f"{s['overall']:.0f}", s["grade"], inf["codec"].upper(),
            f"{inf['width']}×{inf['height']}", f"{inf['fps']:.2f}".rstrip("0").rstrip("."),
            fmt(st["bitrate_mbps"], 1), fmt(st["qp_eq_mean"], 1), fmt(st["cambi_mean"], 2),
            ("cache" if res.get("cache_hit") else "przybliżony" if res.get("approximate") else "gotowe") + (" (!)" if res["warnings"] else "")))
        self.tree.item(iid, tags=(iid,))
        self.tree.tag_configure(iid, foreground=score_color(s["overall"]))
        self.progress["value"] = 1000
        self.lbl_status.config(text=f"Gotowe: {res['name']} – {s['overall']:.0f} pkt "
                                    f"({st['analysis_seconds']} s)")
        sel = self.tree.selection()
        if not sel or sel == (iid,) or self.shown_iid is None:
            self.tree.selection_set(iid)
            self.show(iid)
        self.worker = None
        self._start_next()

    # ------------------------------------------------------------------ widok
    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if sel and sel[0] in self.results:
            self.show(sel[0])

    def show(self, iid):
        res = self.results.get(iid)
        if not res:
            return
        self.shown_iid = iid
        s, st, inf = res["scores"], res["stats"], res["info"]
        self.gauge.set(s["overall"], s["grade"])
        self.bar_qp.set(s["qp"], f"QP śr. {fmt(st['qp_raw_mean'])} ({st['qp_scale']}), "
                                 f"ekw. H.265 {fmt(st['qp_eq_mean'])}")
        self.bar_bits.set(s["bits"], f"{fmt(st['bpp'], 3)} bit/px · SI {fmt(st['si_mean'])} · TI {fmt(st['ti_mean'])}")
        self.bar_cambi.set(s["cambi"], f"CAMBI śr. {fmt(st['cambi_mean'], 2)}, max {fmt(st['cambi_max'], 2)}"
                           if st["cambi_mean"] is not None else "nie liczono")
        self.lbl_name.config(text=res["file"])

        rows = [
            ("Ocena", f"{s['overall']}  (średnia {s['mean']}, najgorsze 5%: {s['p5']})"),
            ("Najsłabszy moment", human_time(s["worst_window_t"]) + f" ({s['worst_window_t']} s)"),
            ("Kodek", f"{inf['codec'].upper()} {inf['profile']}  {inf['pix_fmt']} ({inf['bitdepth']}-bit)"),
            ("Obraz", f"{inf['width']}×{inf['height']} @ {inf['fps']:.3f} fps, {human_time(inf['duration'])}"),
            ("Bitrate", f"{fmt(st['bitrate_mbps'], 2)} Mb/s, plik {human_size(inf['file_size'])}"),
            ("Ramki", f"{st['frames']}  (I {st['count_I']}, P {st['count_P']}, B {st['count_B']}), "
                      f"GOP śr. {fmt(st['gop_avg'])}"),
            ("Rozmiar ramki", f"śr. {human_size(st['frame_size_avg'] or 0)}, max {human_size(st['frame_size_max'])}"),
            ("QP", f"{fmt(st['qp_raw_min'])} – {fmt(st['qp_raw_max'])}, śr. {fmt(st['qp_raw_mean'])}"),
            ("CAMBI", (f"co {st['cambi_subsample']}. klatka, p95 {fmt(st['cambi_p95'], 2)}"
                       if st["cambi_subsample"] else "nie liczono")),
            ("Czas analizy", f"{st['analysis_seconds']} s · {st.get('analysis_fps', 0):.1f} FPS"),
            ("Dekoder", st.get('decoder', 'brak danych')),
            ("Tryb", res.get('mode', 'balanced') + (" — PRZYBLIŻONY" if res.get('approximate') else "")),
            ("Źródło", "Zapisany wcześniej wynik (cache)" if res.get('cache_hit') else "Nowa analiza"),
        ]
        t = self.details
        t.config(state="normal")
        t.delete("1.0", "end")
        for k, v in rows:
            t.insert("end", f"{k:<17}", "k")
            t.insert("end", f"{v}\n")
        for w in res["warnings"]:
            t.insert("end", f"\n! {w}", "w")
        t.config(state="disabled")
        start = time.perf_counter()
        self._charts(res)
        res.setdefault("timings", {})["gui_apply"] = time.perf_counter() - start

    def _charts(self, res):
        specs = self.chart_specs.get(self.shown_iid)
        if specs is None:
            return
        for name in ('ch_score', 'ch_qp', 'ch_size', 'ch_cambi', 'ch_siti'):
            source, chart = getattr(specs, name), getattr(self, name)
            chart.on_draw = lambda seconds, key=name: res.setdefault('timings', {}).setdefault('gui_draw', {}).__setitem__(key, seconds)
            if hasattr(source, 'title'):
                chart.title = source.title
            chart.set(*source.args, **source.kwargs)

    def _sort(self, col):
        items = list(self.tree.get_children())

        def key(i):
            v = self.tree.set(i, col)
            try:
                return (0, float(v.replace("×", "")) if col != "res" else
                        int(v.split("×")[0]) * int(v.split("×")[1]))
            except Exception:
                return (1, v)
        rev = getattr(self, "_sort_rev", {}).get(col, col == "score")
        items.sort(key=key, reverse=rev)
        for n, i in enumerate(items):
            self.tree.move(i, "", n)
        self._sort_rev = {col: not rev}

    def remove_selected(self):
        for iid in self.tree.selection():
            if iid == self.current_iid:
                continue
            self.queue = [q for q in self.queue if q[0] != iid]
            self.results.pop(iid, None)
            self.chart_specs.pop(iid, None)
            self.tree.delete(iid)

    # ------------------------------------------------------------------ eksport
    def export(self, kind):
        res = self.results.get(self.shown_iid)
        if not res:
            messagebox.showinfo(core.APP_NAME, "Najpierw wybierz przeanalizowany plik.")
            return
        base = os.path.splitext(res["name"])[0] + "_ocena"
        p = filedialog.asksaveasfilename(defaultextension=f".{kind}", initialfile=f"{base}.{kind}",
                                         filetypes=[(kind.upper(), f"*.{kind}")])
        if not p:
            return
        try:
            if kind == "json":
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(res, f, ensure_ascii=False, indent=1)
            else:
                core.export_csv(res, p)
            self.lbl_status.config(text=f"Zapisano: {p}")
        except Exception as e:
            messagebox.showerror(core.APP_NAME, str(e))

    def export_table(self):
        if not self.results:
            messagebox.showinfo(core.APP_NAME, "Brak przeanalizowanych plików.")
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="porownanie.csv",
                                         filetypes=[("CSV", "*.csv")])
        if not p:
            return
        import csv
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["plik", "wynik", "ocena", "ocena_qp", "ocena_bitrate", "ocena_cambi", "kodek",
                        "szer", "wys", "fps", "bitdepth", "Mb/s", "bpp", "qp_surowe", "qp_ekw", "SI", "TI",
                        "cambi_sr", "cambi_max", "ramki_I", "ramki_P", "ramki_B", "gop"])
            for iid in self.tree.get_children():
                r = self.results.get(iid)
                if not r:
                    continue
                s, st, i = r["scores"], r["stats"], r["info"]
                w.writerow([r["file"], s["overall"], s["grade"], s["qp"], s["bits"], s["cambi"], i["codec"],
                            i["width"], i["height"], round(i["fps"], 3), i["bitdepth"], st["bitrate_mbps"],
                            st["bpp"], st["qp_raw_mean"], st["qp_eq_mean"], st["si_mean"], st["ti_mean"],
                            st["cambi_mean"], st["cambi_max"], st["count_I"], st["count_P"], st["count_B"],
                            st["gop_avg"]])
        self.lbl_status.config(text=f"Zapisano: {p}")

    # ------------------------------------------------------------------ pomoc
    def show_help(self):
        HelpWindow(self)

    def show_about(self):
        messagebox.showinfo(core.APP_NAME, f"{core.APP_NAME} {core.APP_VERSION}\n\n"
                            "Ocena jakości kompresji H.264 / H.265 / AV1 / VP9 bez pliku źródłowego.\n"
                            "Silnik: ffmpeg (trace_headers, siti, libvmaf CAMBI).\n\n"
                            f"{self.tools.describe()}")


HELP_TEXT = """JAK LICZONA JEST OCENA (0–100)

Program nie potrzebuje oryginału. Ocenia plik na podstawie trzech źródeł informacji:

1. KWANTYZACJA (QP) – waga 70%
   Odczytywana wprost z nagłówków strumienia, bez dekodowania obrazu:
     • H.265 / H.264: slice QP (0–51)  = 26 + init_qp + slice_qp_delta
     • AV1 / VP9: base_q_idx (0–255), przeliczany na QP-ekwiwalent H.265
   Im wyższy QP, tym grubsza kwantyzacja i więcej utraconych detali.
   H.264 dostaje +3 do QP (przy tym samym QP wygląda gorzej niż H.265).

2. BITRATE vs ZŁOŻONOŚĆ – waga 30%
   Bity na piksel każdej ramki, przeliczone na ekwiwalent H.264 (wydajność kodeka)
   i porównane z tym, ile bitów potrzebuje dana treść. Złożoność treści mierzy
   SI/TI z normy ITU-T P.910 (SI = szczegóły, TI = ruch). Ten sam bitrate na
   statycznym ujęciu i na zjeździe przez las daje inne oceny.

3. BANDING (CAMBI, Netflix) – tylko kara
   Wykrywa pasmowanie w gradientach (niebo, ściany) – typowy artefakt AV1/HEVC.
   0 = brak, ~5 = zaczyna być widoczny. Brak bandingu nie podnosi oceny,
   jego obecność ją obniża.

Ocena jest liczona w oknach 1-sekundowych. Wynik końcowy to
75% średniej + 25% najgorszych 5% okien – kompresja zwykle „pęka” w momentach
dużego ruchu i sama średnia by to ukryła.

Skala: 90+ doskonała · 75+ bardzo dobra · 60+ dobra · 40+ przeciętna · 20+ słaba

KALIBRACJA
Krzywe dopasowano tak, by kolejność i poziom ocen odpowiadały VMAF dla H.264,
H.265 i AV1 przy różnych CRF. To heurystyka, nie norma – wszystkie progi są
w pliku scoring.json (menu Ustawienia) i można je dostroić do własnego materiału.

OGRANICZENIA
• Ocenia ślady kompresji, nie ostrość obiektywu, ekspozycję czy szum sensora.
• Bardzo zaszumiony materiał podnosi QP i bitrate przy dobrym obrazie –
  porównuj kamery na tej samej scenie.
• Enkodery sprzętowe w kamerach mogą używać adaptacyjnej kwantyzacji per blok;
  odczytywany jest QP plastra/ramki, nie każdego bloku.
"""


class HelpWindow(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Jak liczona jest ocena")
        self.configure(bg=C["panel"])
        self.geometry("720x640")
        t = tk.Text(self, bg=C["panel"], fg=C["fg"], relief="flat", wrap="word", font=(FONT, 10),
                    padx=18, pady=14, highlightthickness=0)
        t.insert("1.0", HELP_TEXT)
        t.config(state="disabled")
        t.pack(fill="both", expand=True)


def main():
    args = sys.argv[1:]
    if args and args[0] == "--cli":
        core.cli(args[1:])
        return
    if core.IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    app = App([a for a in args if os.path.isfile(a)])
    app.mainloop()


if __name__ == "__main__":
    main()
