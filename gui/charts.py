from __future__ import annotations

import math
import threading
import time
import tkinter as tk
from bisect import bisect_left

from .theme import C, FONT
from .widgets import human_time

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
