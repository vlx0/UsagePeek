"""UsagePeek GUI."""

from __future__ import annotations

import ctypes
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

from . import __version__, settings
from .instance import WINDOW_TITLE
from .scan import SourceStat, collect

BG = "#111111"
FG = "#ffffff"
MUTED = "#999999"
CARD = "#1a1a1a"
BTN = "#333333"
ACCENT = "#93c5fd"
OK = "#86efac"
LOSE = "#fca5a5"
LINE = "#2a2a2a"


def _fmt_tok(n: int) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_bytes(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} ГБ"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.0f} МБ"
    return f"{n} Б"


def _fmt_usd(x: float) -> str:
    if x <= 0:
        return ""
    if x < 0.01:
        return f"${x:.4f}"
    return f"${x:.2f}"


class UsagePeekApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(WINDOW_TITLE)
        self.geometry("740x680")
        self.minsize(560, 480)
        self.configure(bg=BG)
        self._period = tk.StringVar(value="today")

        pad = tk.Frame(self, bg=BG)
        pad.pack(fill="both", expand=True, padx=22, pady=16)

        head = tk.Frame(pad, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="UsagePeek", bg=BG, fg=FG, font=("Segoe UI Semibold", 20)).pack(side="left")
        tk.Label(
            head, text="неделя 7 · ИИ · день 46", bg=BG, fg=MUTED, font=("Segoe UI", 9)
        ).pack(side="right", pady=(10, 0))

        tk.Label(
            pad,
            text="Локальный расход AI-инструментов на этом ПК. Ничего не отправляем — "
            "только файлы на диске и проверка localhost-порта у локальных движков.",
            bg=BG, fg=MUTED, font=("Segoe UI", 9), wraplength=680, justify="left",
        ).pack(anchor="w", pady=(6, 12))

        per = tk.Frame(pad, bg=BG)
        per.pack(fill="x")
        for key, title in (("today", "Сегодня"), ("7d", "7 дней"), ("all", "Всё")):
            tk.Radiobutton(
                per, text=title, value=key, variable=self._period,
                command=self._refresh, bg=BG, fg=FG, selectcolor=CARD,
                activebackground=BG, activeforeground=FG, font=("Segoe UI", 10),
            ).pack(side="left", padx=(0, 12))
        tk.Button(
            per, text="Обновить", command=self._refresh,
            bg=BTN, fg=FG, relief="flat", padx=12, pady=4, cursor="hand2",
        ).pack(side="right")
        tk.Button(
            per, text="Добавить локальную ИИ…", command=self._add_local,
            bg=BTN, fg=FG, relief="flat", padx=12, pady=4, cursor="hand2",
        ).pack(side="right", padx=(0, 8))

        wrap = tk.Frame(pad, bg=BG)
        wrap.pack(fill="both", expand=True, pady=(10, 0))
        self._canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        bar = tk.Scrollbar(wrap, orient="vertical", command=self._canvas.yview)
        self._canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self._canvas.configure(yscrollcommand=bar.set)
        self._list = tk.Frame(self._canvas, bg=BG)
        self._win = self._canvas.create_window((0, 0), window=self._list, anchor="nw")
        self._list.bind("<Configure>", lambda _e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(self._win, width=e.width))
        self.bind_all("<MouseWheel>", lambda e: self._canvas.yview_scroll(int(-e.delta / 120), "units"))

        foot = tk.Frame(pad, bg=BG)
        foot.pack(fill="x", pady=(8, 0))
        tk.Label(foot, text=f"v{__version__} · darkshade", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="right")

        self.after(20, self._place)
        self._refresh()

    def _place(self) -> None:
        self.update_idletasks()
        w, h = self.winfo_width() or 740, self.winfo_height() or 680
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 4}")

    # ------------------------------------------------------------- custom

    def _add_local(self) -> None:
        folder = filedialog.askdirectory(
            title="Папка локальной ИИ (модели, база или логи)",
            mustexist=True,
        )
        if not folder:
            return
        path = Path(folder)
        if any(s.path == str(path) for s in settings.load()):
            messagebox.showinfo("UsagePeek", "Эта папка уже добавлена.")
            return
        settings.add(path.name or str(path), str(path))
        self._refresh()

    def _remove_local(self, source_id: str, label: str) -> None:
        if not messagebox.askyesno("UsagePeek", f"Убрать «{label}» из списка?"):
            return
        settings.remove(source_id)
        self._refresh()

    # ------------------------------------------------------------- render

    def _refresh(self) -> None:
        for w in self._list.winfo_children():
            w.destroy()
        try:
            rows = collect(self._period.get())
        except Exception as exc:
            tk.Label(self._list, text=str(exc), bg=BG, fg=LOSE, wraplength=640, justify="left").pack(anchor="w")
            return

        agents = [r for r in rows if r.kind == "agent"]
        locals_ = [r for r in rows if r.kind == "local"]
        if agents:
            self._section("АГЕНТЫ И IDE")
            for st in agents:
                self._card(st)
        self._section("ЛОКАЛЬНЫЕ МОДЕЛИ")
        for st in locals_:
            self._card(st)
        if not any(s.removable for s in locals_):
            tk.Label(
                self._list,
                text="Свой движок (Jan, GPT4All, llama.cpp…) — «Добавить локальную ИИ…» "
                "и указать папку с моделями или базой.",
                bg=BG, fg=MUTED, font=("Segoe UI", 8), wraplength=640, justify="left",
            ).pack(anchor="w", pady=(4, 0))

    def _section(self, title: str) -> None:
        tk.Label(
            self._list, text=title, bg=BG, fg=FG, font=("Segoe UI Semibold", 10)
        ).pack(anchor="w", pady=(12, 2))

    def _card(self, st: SourceStat) -> None:
        box = tk.Frame(self._list, bg=CARD)
        box.pack(fill="x", pady=5, ipady=8, ipadx=10)

        top = tk.Frame(box, bg=CARD)
        top.pack(fill="x", padx=10, pady=(6, 0))
        tk.Label(top, text=st.label, bg=CARD, fg=FG, font=("Segoe UI Semibold", 12)).pack(side="left")
        if st.removable:
            tk.Button(
                top, text="✕", command=lambda: self._remove_local(st.id, st.label),
                bg=CARD, fg=MUTED, relief="flat", bd=0, padx=4, cursor="hand2",
                activebackground=CARD, activeforeground=LOSE, font=("Segoe UI", 10),
            ).pack(side="right", padx=(8, 0))
        if st.running is True:
            tk.Label(top, text="сервер запущен", bg=CARD, fg=OK, font=("Segoe UI", 9)).pack(side="right")
        elif st.running is False and st.found:
            tk.Label(top, text="сервер выключен", bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(side="right")
        else:
            badge = "найдено" if st.found else "нет на диске"
            tk.Label(
                top, text=badge, bg=CARD, fg=OK if st.found else MUTED, font=("Segoe UI", 9)
            ).pack(side="right")

        if st.error:
            tk.Label(box, text=st.error, bg=CARD, fg=LOSE, font=("Segoe UI", 9), wraplength=620, justify="left").pack(
                anchor="w", padx=10, pady=4
            )
            return
        if not st.found:
            tk.Label(box, text=st.path or "—", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w", padx=10, pady=(2, 6))
            return

        bits: list[str] = []
        if st.model_files:
            bits.append(f"моделей {st.model_files}")
        if st.model_bytes:
            bits.append(f"на диске {_fmt_bytes(st.model_bytes)}")
        if st.sessions:
            bits.append(f"сессий {st.sessions}")
        if st.total_tokens:
            bits.append(f"in {_fmt_tok(st.input_tokens)}")
            bits.append(f"out {_fmt_tok(st.output_tokens)}")
            if st.cache_tokens:
                bits.append(f"cache {_fmt_tok(st.cache_tokens)}")
        usd = _fmt_usd(st.cost_usd)
        if usd:
            bits.append(usd)
        if bits:
            tk.Label(
                box, text="   ".join(bits), bg=CARD, fg=ACCENT, font=("Consolas", 10),
                wraplength=620, justify="left",
            ).pack(anchor="w", padx=10, pady=(4, 0))

        if st.extra:
            tk.Label(box, text=st.extra, bg=CARD, fg=FG, font=("Segoe UI", 9)).pack(anchor="w", padx=10)
        if st.models:
            models = " · ".join(m if t == 0 else f"{m} {_fmt_tok(t)}" for m, t in st.models[:5])
            tk.Label(box, text=models, bg=CARD, fg=MUTED, font=("Segoe UI", 8), wraplength=620, justify="left").pack(
                anchor="w", padx=10
            )
        if st.note:
            tk.Label(box, text=st.note, bg=CARD, fg=MUTED, font=("Segoe UI", 8), wraplength=620, justify="left").pack(
                anchor="w", padx=10, pady=(2, 6)
            )
        else:
            tk.Frame(box, bg=CARD, height=6).pack()


def run() -> None:
    UsagePeekApp().mainloop()
