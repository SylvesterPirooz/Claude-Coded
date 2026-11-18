#!/usr/bin/env python3
"""
Window version of the Danbooru tag downloader.

    python3 danbooru_gui.py

Needs danbooru_dl.py in the same folder. Uses only the standard library (tkinter).
"""

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from danbooru_dl import (  # noqa: E402
    DEFAULT_PRESET, MAX_GAP, MAX_WORKERS, MIN_GAP, SITE_PAGE_SIZE, SPEED_PRESETS,
    DownloadError, LiveStats, make_auth, run_download,
)

DEFAULT_OUT = os.path.expanduser("~/Pictures/Danbooru")
RATE_WINDOW = 10.0          # seconds of history used for MB/s, files/min and time left


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events = queue.Queue()      # worker thread -> GUI messages
        self.stop_flag = threading.Event()
        self.worker = None
        self.last_folder = None
        self.live = None                 # LiveStats of the running download
        self.samples = deque()           # (time, bytes, files, completed) for rates
        self._syncing = False            # guard while a preset fills in the numbers

        root.title("Danbooru Downloader")
        root.minsize(640, 560)

        self.url = tk.StringVar()
        self.out = tk.StringVar(value=DEFAULT_OUT)
        self.limit_mode = tk.StringVar(value="all")   # "all", "images" or "pages"
        self.image_count = tk.IntVar(value=50)
        self.page_count = tk.IntVar(value=1)
        self.dry_run = tk.BooleanVar(value=False)
        self.login = tk.StringVar(value=os.environ.get("DANBOORU_LOGIN", ""))
        self.api_key = tk.StringVar(value=os.environ.get("DANBOORU_API_KEY", ""))
        self.status = tk.StringVar(value="Paste a Danbooru search URL and press Start.")

        workers, gap = SPEED_PRESETS[DEFAULT_PRESET]
        self.preset = tk.StringVar(value=DEFAULT_PRESET)
        self.workers = tk.StringVar(value=str(workers))
        self.gap = tk.StringVar(value=f"{gap:g}")
        self.speed_desc = tk.StringVar()

        self._build()
        self.workers.trace_add("write", self._numbers_changed)
        self.gap.trace_add("write", self._numbers_changed)
        self._update_speed_desc()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll_events)

    # ---------- layout ----------
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)

        # Search URL
        ttk.Label(main, text="Search URL:").grid(row=0, column=0, sticky="w", **pad)
        url_entry = ttk.Entry(main, textvariable=self.url)
        url_entry.grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(main, text="Paste", command=self.paste_url).grid(row=0, column=2, **pad)
        url_entry.focus()

        # Output folder
        ttk.Label(main, text="Save into:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(main, textvariable=self.out).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(main, text="Browse…", command=self.browse).grid(row=1, column=2, **pad)
        ttk.Label(main, text="A subfolder named after the tags is created here.",
                  foreground="gray").grid(row=2, column=1, sticky="w", padx=8)

        # Options
        opts = ttk.LabelFrame(main, text="Options", padding=8)
        opts.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)

        ttk.Label(opts, text="How much to save:").grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Radiobutton(opts, text="Everything in the search", value="all",
                        variable=self.limit_mode, command=self._toggle_max
                        ).grid(row=1, column=0, sticky="w", padx=(12, 0))

        ttk.Radiobutton(opts, text="First", value="images",
                        variable=self.limit_mode, command=self._toggle_max
                        ).grid(row=2, column=0, sticky="w", padx=(12, 0))
        self.image_spin = ttk.Spinbox(opts, from_=1, to=1_000_000, increment=10,
                                      textvariable=self.image_count, width=8)
        self.image_spin.grid(row=2, column=1, sticky="w", padx=4)
        ttk.Label(opts, text="images").grid(row=2, column=2, sticky="w")

        ttk.Radiobutton(opts, text="First", value="pages",
                        variable=self.limit_mode, command=self._toggle_max
                        ).grid(row=3, column=0, sticky="w", padx=(12, 0))
        self.page_spin = ttk.Spinbox(opts, from_=1, to=50_000, increment=1,
                                     textvariable=self.page_count, width=8)
        self.page_spin.grid(row=3, column=1, sticky="w", padx=4)
        ttk.Label(opts, text=f"pages  ({SITE_PAGE_SIZE} images per page, like the website)"
                  ).grid(row=3, column=2, sticky="w")

        ttk.Checkbutton(opts, text="Dry run (just list files, download nothing)",
                        variable=self.dry_run).grid(row=4, column=0, columnspan=3,
                                                    sticky="w", pady=(8, 0))

        # Speed
        speed = ttk.LabelFrame(main, text="Download speed", padding=8)
        speed.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        presets_row = ttk.Frame(speed)
        presets_row.grid(row=0, column=0, sticky="w")
        self.speed_widgets = []
        for name in [*SPEED_PRESETS, "Custom"]:
            rb = ttk.Radiobutton(presets_row, text=name, value=name, variable=self.preset,
                                 command=self._preset_chosen)
            rb.pack(side="left", padx=(0, 14))
            self.speed_widgets.append(rb)
        self.adv_btn = ttk.Button(presets_row, text="Advanced ▸", command=self._toggle_advanced)
        self.adv_btn.pack(side="left")
        ttk.Label(speed, textvariable=self.speed_desc, foreground="gray"
                  ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        self.adv = ttk.Frame(speed)       # hidden until "Advanced ▸" is clicked
        ttk.Label(self.adv, text="Parallel downloads:").grid(row=0, column=0, sticky="w")
        sp_w = ttk.Spinbox(self.adv, from_=1, to=MAX_WORKERS, increment=1,
                           textvariable=self.workers, width=6)
        sp_w.grid(row=0, column=1, sticky="w", padx=(4, 16))
        ttk.Label(self.adv, text="Delay between starts (s):").grid(row=0, column=2, sticky="w")
        sp_g = ttk.Spinbox(self.adv, from_=MIN_GAP, to=MAX_GAP, increment=0.05,
                           textvariable=self.gap, width=6, format="%.2f")
        sp_g.grid(row=0, column=3, sticky="w", padx=4)
        ttk.Label(self.adv, text=f"Limits: 1–{MAX_WORKERS} downloads, {MIN_GAP}–{MAX_GAP} s.",
                  foreground="gray").grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.speed_widgets += [sp_w, sp_g, self.adv_btn]

        # Login (optional)
        acct = ttk.LabelFrame(main, text="Danbooru account (optional)", padding=8)
        acct.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)
        acct.columnconfigure(1, weight=1)
        acct.columnconfigure(3, weight=1)
        ttk.Label(acct, text="Username:").grid(row=0, column=0, sticky="w")
        ttk.Entry(acct, textvariable=self.login).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(acct, text="API key:").grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Entry(acct, textvariable=self.api_key, show="•").grid(row=0, column=3,
                                                                   sticky="ew", padx=4)

        # Buttons
        btns = ttk.Frame(main)
        btns.grid(row=6, column=0, columnspan=3, sticky="ew", **pad)
        self.start_btn = ttk.Button(btns, text="Start", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(btns, text="Open folder", command=self.open_folder,
                                   state="disabled")
        self.open_btn.pack(side="left")
        ttk.Button(btns, text="Clear log", command=self.clear_log).pack(side="right")

        # Progress
        self.bar = ttk.Progressbar(main, mode="determinate")
        self.bar.grid(row=7, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Label(main, textvariable=self.status).grid(row=8, column=0, columnspan=3,
                                                       sticky="w", padx=8)

        # Log
        self.log_box = ScrolledText(main, height=12, state="disabled", wrap="none",
                                    font=("monospace", 9))
        self.log_box.grid(row=9, column=0, columnspan=3, sticky="nsew", **pad)
        main.rowconfigure(9, weight=1)

        self._toggle_max()

    def _toggle_max(self):
        mode = self.limit_mode.get()
        self.image_spin.configure(state="normal" if mode == "images" else "disabled")
        self.page_spin.configure(state="normal" if mode == "pages" else "disabled")

    # ---------- speed settings ----------
    def _read_speed(self):
        """Current (workers, gap) from the number boxes, or None if they're not valid."""
        try:
            workers = int(self.workers.get())
            gap = float(self.gap.get())
        except ValueError:
            return None
        if not (1 <= workers <= MAX_WORKERS and MIN_GAP - 1e-9 <= gap <= MAX_GAP + 1e-9):
            return None
        return workers, gap

    def _preset_chosen(self):
        name = self.preset.get()
        if name in SPEED_PRESETS:
            workers, gap = SPEED_PRESETS[name]
            self._syncing = True
            self.workers.set(str(workers))
            self.gap.set(f"{gap:g}")
            self._syncing = False
        elif not self.adv.winfo_ismapped():
            self._toggle_advanced()          # "Custom" opens the number boxes
        self._update_speed_desc()

    def _numbers_changed(self, *_):
        if self._syncing:
            return
        speed = self._read_speed()
        match = next((n for n, v in SPEED_PRESETS.items()
                      if speed and v[0] == speed[0] and abs(v[1] - speed[1]) < 1e-9), None)
        self.preset.set(match or "Custom")
        self._update_speed_desc()

    def _update_speed_desc(self):
        speed = self._read_speed()
        if speed is None:
            self.speed_desc.set(f"Enter 1–{MAX_WORKERS} downloads and a delay of "
                                f"{MIN_GAP}–{MAX_GAP} s.")
            return
        workers, gap = speed
        at_once = "1 at a time" if workers == 1 else f"{workers} at once"
        per_sec = 1 / gap
        rate = f"{per_sec:.0f}" if abs(per_sec - round(per_sec)) < 0.05 else f"{per_sec:.1f}"
        noun = "file" if rate == "1" else "files"
        self.speed_desc.set(f"{at_once} · up to {rate} new {noun} per second")

    def _toggle_advanced(self):
        if self.adv.winfo_ismapped():
            self.adv.grid_remove()
            self.adv_btn.configure(text="Advanced ▸")
        else:
            self.adv.grid(row=2, column=0, sticky="w", pady=(8, 0))
            self.adv_btn.configure(text="Advanced ▾")

    # ---------- button actions ----------
    def paste_url(self):
        try:
            self.url.set(self.root.clipboard_get().strip())
        except tk.TclError:
            pass

    def browse(self):
        start = self.out.get() if os.path.isdir(self.out.get()) else os.path.expanduser("~")
        folder = filedialog.askdirectory(initialdir=start, title="Choose where to save")
        if folder:
            self.out.set(folder)

    def start(self):
        url = self.url.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Paste a Danbooru search URL first.")
            return
        max_posts = max_pages = 0
        mode = self.limit_mode.get()
        if mode != "all":
            var, what = ((self.image_count, "images") if mode == "images"
                         else (self.page_count, "pages"))
            try:
                n = int(var.get())
                if n < 1:
                    raise ValueError
            except (ValueError, tk.TclError):
                messagebox.showwarning("Bad number", f"Enter how many {what} to save (1 or more).")
                return
            if mode == "images":
                max_posts = n
            else:
                max_pages = n

        speed = self._read_speed()
        if speed is None:
            messagebox.showwarning("Bad speed setting",
                                   f"Parallel downloads must be 1–{MAX_WORKERS} and the delay "
                                   f"{MIN_GAP}–{MAX_GAP} seconds.")
            return
        workers, gap = speed

        self.stop_flag.clear()
        self.live = LiveStats()
        self.samples.clear()
        self.bar.configure(mode="indeterminate", value=0)
        self.bar.start(15)
        self.status.set("Starting…")
        self._set_running(True)

        settings = dict(
            url=url,
            out=self.out.get().strip() or DEFAULT_OUT,
            max_posts=max_posts,
            max_pages=max_pages,
            dry_run=self.dry_run.get(),
            auth=make_auth(self.login.get().strip(), self.api_key.get().strip()),
            workers=workers,
            gap=gap,
        )
        self.worker = threading.Thread(target=self._work, kwargs=settings, daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_flag.set()
        self.status.set("Stopping once the downloads in progress finish…")
        self.stop_btn.configure(state="disabled")

    def open_folder(self):
        if self.last_folder and os.path.isdir(self.last_folder):
            opener = {"win32": "explorer", "darwin": "open"}.get(sys.platform, "xdg-open")
            subprocess.Popen([opener, self.last_folder])

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Download running", "Stop the download and quit?"):
                return
            self.stop_flag.set()
        self.root.destroy()

    # ---------- background work ----------
    def _work(self, **settings):
        """Runs in a separate thread so the window doesn't freeze."""
        try:
            stats = run_download(
                **settings,
                log=lambda m: self.events.put(("log", m)),
                live=self.live,
                should_stop=self.stop_flag.is_set,
            )
            self.events.put(("finished", stats))
        except DownloadError as e:
            self.events.put(("error", str(e)))
        except Exception as e:  # anything unexpected still shows up in the window
            self.events.put(("error", f"{type(e).__name__}: {e}"))

    def poll_events(self):
        """Move messages from the worker threads onto the screen (tkinter isn't thread-safe)."""
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self._append_log(data)
                elif kind == "finished":
                    self._finish(data)
                elif kind == "error":
                    self._append_log(f"ERROR: {data}")
                    self._finish(None)
                    messagebox.showerror("Download failed", data)
        except queue.Empty:
            pass
        if self.worker and self.worker.is_alive() and not self.stop_flag.is_set():
            self._show_live()
        self.root.after(250, self.poll_events)

    def _append_log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _show_live(self):
        """Progress bar plus 'posts · MB/s · files/min · time left' while running."""
        live = self.live
        if live is None or live.total is None and live.completed == 0:
            return
        now = time.monotonic()
        self.samples.append((now, live.bytes, live.files, live.completed))
        while len(self.samples) > 2 and now - self.samples[0][0] > RATE_WINDOW:
            self.samples.popleft()

        total, done = live.total, live.completed
        if total:
            if str(self.bar["mode"]) != "determinate":
                self.bar.stop()
                self.bar.configure(mode="determinate")
            self.bar.configure(maximum=total, value=min(done, total))
        parts = [f"{done} / {total} posts" if total else f"{done} posts"]

        if not self.dry_run.get():
            t0, b0, f0, c0 = self.samples[0]
            span = now - t0
            if span >= 1.5:
                mb_s = (live.bytes - b0) / span / 1_048_576
                files_min = (live.files - f0) / span * 60
                parts += [f"{mb_s:.1f} MB/s", f"{files_min:.0f} files/min"]
                # Use real download speed, not posts/s: files you already have
                # are skipped instantly and would make the estimate far too low.
                files_s = (live.files - f0) / span
                if total and files_s > 0:
                    left = (total - done) / files_s
                    parts.append("a few seconds left" if left < 5
                                 else f"about {fmt_duration(left)} left")
            else:
                parts.append("measuring speed…")
        self.status.set(" · ".join(parts))

    def _finish(self, stats):
        self.bar.stop()
        self.bar.configure(mode="determinate")
        self._set_running(False)
        if stats is None:
            self.status.set("Failed. See the log.")
            return
        self.last_folder = stats["folder"]
        if os.path.isdir(self.last_folder):
            self.open_btn.configure(state="normal")
        if self.live and self.live.total:
            self.bar.configure(maximum=self.live.total,
                               value=min(self.live.completed, self.live.total))
        verb = "Would download" if self.dry_run.get() else "Downloaded"
        msg = (f"{'Stopped' if stats['stopped'] else 'Done'}. {verb} {stats['done']}, "
               f"already had {stats['skipped']}, unavailable {stats['unavailable']}, "
               f"failed {stats['failed']}")
        if self.live and self.live.bytes and not self.dry_run.get():
            elapsed = time.monotonic() - self.live.started
            msg += (f" · {self.live.bytes / 1_048_576:.0f} MB in {fmt_duration(elapsed)}"
                    f" ({self.live.bytes / max(elapsed, 0.001) / 1_048_576:.1f} MB/s avg)")
        self.status.set(msg + ".")

    def _set_running(self, running):
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        # speed can't change mid-run, so lock those controls while downloading
        for w in self.speed_widgets:
            w.configure(state="disabled" if running else "normal")


def main():
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
