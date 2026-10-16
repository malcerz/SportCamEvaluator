from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .theme import C, FONT, MONO

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
