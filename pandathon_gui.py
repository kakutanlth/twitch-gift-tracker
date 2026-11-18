"""
Pandathon tracker window
========================
A window around pandathon_tracker.py: buttons and settings in the top half, the live log in the bottom half.

  Start / Stop tracking   listen to chat and update the sheet (the same as run_tracker.bat)
  Refresh                 reconnect to chat and write any saved events to the sheet now
  Sheet config            choose the Google Sheet and the sheet (tab) to write to; Open sheet opens it in the browser
  Delete row              empty one person's row (by row number or username); the rows below move up
  Light / Dark            switch the window's colours (top right); the choice is remembered
  Settings                the channel name and the cheer minimum, saved to config.json

Start it by double-clicking run_tracker_gui.bat.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

import pandathon_tracker as core

APP_TITLE = "Pandathon Tracker"
CONFIG_PATH = core.BASE / "config.json"
WINDOW_PREFS = core.BASE / "data" / "window.json"
MAX_LOG_LINES = 5000
POLL_MS = 150

log = logging.getLogger("tracker")

THEMES = {
    "light": {
        "bg": "#f4f5f2", "surface": "#ffffff", "fg": "#1c2420", "muted": "#5d6a63", "border": "#cfd5cf",
        "button": "#e6eae5", "button_hover": "#d9dfd8", "accent": "#1f7a4d", "accent_hover": "#18643f",
        "accent_fg": "#ffffff", "danger": "#b3261e", "danger_hover": "#962019", "danger_fg": "#ffffff",
        "disabled": "#9aa39d", "select": "#cfe5d8", "warning": "#a15c00", "error": "#b3261e",
    },
    "dark": {
        "bg": "#121614", "surface": "#1a201d", "fg": "#e6ebe7", "muted": "#9aa8a0", "border": "#2f3833",
        "button": "#242b27", "button_hover": "#2e3732", "accent": "#5fc48f", "accent_hover": "#78d3a3",
        "accent_fg": "#0d1a13", "danger": "#ff8a80", "danger_hover": "#ffa49c", "danger_fg": "#2a0f0c",
        "disabled": "#5f6b65", "select": "#2f4a3b", "warning": "#f3c06b", "error": "#ff8a80",
    },
}


class QueueLogHandler(logging.Handler):
    """Hands log lines to the window. Only the window's own thread may touch the widgets."""

    def __init__(self, lines: queue.Queue):
        super().__init__()
        self.lines = lines

    def emit(self, record):
        try:
            self.lines.put((record.levelno, self.format(record)))
        except Exception:
            self.handleError(record)


def setup_logging(lines: queue.Queue):
    """Log to the window and to data/tracker.log (the same file the command line uses)."""
    (core.BASE / "data").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (QueueLogHandler(lines), logging.FileHandler(core.BASE / "data" / "tracker.log", encoding="utf-8")):
        handler.setFormatter(fmt)
        root.addHandler(handler)


# --------------------------------------------------------------------------- theme

def windows_prefers_dark() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def load_theme() -> str:
    """The theme picked last time, or Windows' own app theme the first time."""
    try:
        theme = json.loads(WINDOW_PREFS.read_text(encoding="utf-8")).get("theme")
    except (OSError, ValueError):
        theme = None
    if theme in THEMES:
        return theme
    return "dark" if windows_prefers_dark() else "light"


def save_theme(theme: str):
    try:
        WINDOW_PREFS.write_text(json.dumps({"theme": theme}), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not remember the theme (%s).", exc)


def set_title_bar_dark(window, dark: bool):
    """Windows 10/11 draw the title bar themselves; ask for the dark one to match."""
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x37)  # redraw the frame now
    except Exception:
        pass


def style_window(style: ttk.Style, root: tk.Tk, p: dict):
    style.theme_use("clam")  # the native Windows theme ignores colours
    style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["surface"],
                    bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["bg"],
                    focuscolor=p["accent"], selectbackground=p["select"], selectforeground=p["fg"],
                    insertcolor=p["fg"], arrowcolor=p["fg"], font=("Segoe UI", 10))
    style.map(".", foreground=[("disabled", p["disabled"])])
    style.configure("Headline.TLabel", font=("Segoe UI", 15, "bold"))
    style.configure("Title.TLabel", font=("Segoe UI", 12, "bold"))
    style.configure("Detail.TLabel", foreground=p["muted"])
    style.configure("TLabelframe", bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"])
    style.configure("TLabelframe.Label", foreground=p["muted"])

    def button(name, base, hover, fg, border, **extra):
        style.configure(name, background=base, foreground=fg, bordercolor=border, lightcolor=base, darkcolor=base, **extra)
        states = [("disabled", p["bg"]), ("pressed", hover), ("active", hover)]
        style.map(name, background=states, lightcolor=states, darkcolor=states,
                  foreground=[("disabled", p["disabled"])], bordercolor=[("disabled", p["border"])])

    button("TButton", p["button"], p["button_hover"], p["fg"], p["border"], padding=(12, 6))
    button("Tool.TButton", p["button"], p["button_hover"], p["fg"], p["border"], padding=(14, 8))
    button("Big.TButton", p["accent"], p["accent_hover"], p["accent_fg"], p["accent"],
           padding=(18, 10), font=("Segoe UI", 11, "bold"))
    button("Accent.TButton", p["accent"], p["accent_hover"], p["accent_fg"], p["accent"],
           padding=(14, 6), font=("Segoe UI", 10, "bold"))
    button("Danger.TButton", p["danger"], p["danger_hover"], p["danger_fg"], p["danger"],
           padding=(14, 6), font=("Segoe UI", 10, "bold"))

    for name in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(name, fieldbackground=p["surface"], foreground=p["fg"], background=p["button"],
                        bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"],
                        insertcolor=p["fg"], arrowcolor=p["fg"])
        style.map(name, fieldbackground=[("disabled", p["bg"]), ("readonly", p["surface"])],
                  foreground=[("disabled", p["disabled"])], bordercolor=[("focus", p["accent"])],
                  selectbackground=[("readonly", p["surface"])], selectforeground=[("readonly", p["fg"])])
    style.configure("Placeholder.TCombobox", foreground=p["muted"])
    style.map("Placeholder.TCombobox", foreground=[("readonly", p["muted"])])
    style.configure("Vertical.TScrollbar", background=p["button"], troughcolor=p["bg"], bordercolor=p["bg"],
                    lightcolor=p["button"], darkcolor=p["button"], arrowcolor=p["muted"])
    style.map("Vertical.TScrollbar", background=[("active", p["button_hover"])])

    for option, value in (("background", p["surface"]), ("foreground", p["fg"]),
                          ("selectBackground", p["select"]), ("selectForeground", p["fg"])):
        root.option_add(f"*TCombobox*Listbox.{option}", value)


# --------------------------------------------------------------------------- main window

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.lines = queue.Queue()
        self.ui_calls = queue.Queue()  # work finished in the background, to be shown by the window's thread
        self.tracker = None
        self.state = "stopped"  # stopped / starting / running / stopping; worker threads change it too
        self.delete_dialog = None
        self.sheet_dialog = None
        self._lock = threading.Lock()
        self._close_when_stopped = False
        setup_logging(self.lines)

        root.title(APP_TITLE)
        scale = root.winfo_fpixels("1i") / 96
        root.geometry(f"{int(800 * scale)}x{int(640 * scale)}")
        root.minsize(int(640 * scale), int(500 * scale))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = lambda *exc: log.error("Window error", exc_info=exc)

        self.style = ttk.Style(root)
        self._build()
        self.apply_theme(load_theme())
        root.after(100, lambda: set_title_bar_dark(root, self.theme == "dark"))
        self._load_settings()
        log.info("Ready. Press Start tracking to begin.")
        self._poll()

    # ------------------------------------------------------------------ layout

    def _build(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1, uniform="half")  # top half: status, buttons, settings
        self.root.rowconfigure(1, weight=1, uniform="half")  # bottom half: log

        top = ttk.Frame(self.root, padding=(16, 14, 16, 8))
        top.grid(row=0, column=0, sticky="nsew")
        top.columnconfigure(0, weight=1)

        self.headline = ttk.Label(top, text="Stopped", style="Headline.TLabel")
        self.headline.grid(row=0, column=0, sticky="w")
        self.theme_button = ttk.Button(top, style="TButton", command=self.toggle_theme)
        self.theme_button.grid(row=0, column=1, sticky="ne")
        self.detail = ttk.Label(top, text="", style="Detail.TLabel")
        self.detail.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 12))

        buttons = ttk.Frame(top)
        buttons.grid(row=2, column=0, columnspan=2, sticky="w")
        self.start_button = ttk.Button(buttons, text="Start tracking", style="Big.TButton", width=16, command=self.toggle)
        self.start_button.grid(row=0, column=0, padx=(0, 10))
        self.refresh_button = ttk.Button(buttons, text="Refresh", style="Tool.TButton", command=self.refresh)
        self.refresh_button.grid(row=0, column=1, padx=(0, 10))
        ttk.Button(buttons, text="Sheet config", style="Tool.TButton", command=self.open_sheet_dialog).grid(row=0, column=2, padx=(0, 10))
        self.delete_button = ttk.Button(buttons, text="Delete row ...", style="Tool.TButton", command=self.open_delete_dialog)
        self.delete_button.grid(row=0, column=3)

        settings = ttk.LabelFrame(top, text="Settings", padding=(12, 8))
        settings.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        settings.columnconfigure(1, weight=1)
        self.channel_var = tk.StringVar()
        self.min_bits_var = tk.StringVar()
        ttk.Label(settings, text="Twitch channel").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
        self.channel_entry = ttk.Entry(settings, textvariable=self.channel_var, width=32)
        self.channel_entry.grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(settings, text="Cheer minimum (bits)").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
        self.min_bits_box = ttk.Spinbox(settings, textvariable=self.min_bits_var, from_=1, to=1_000_000, increment=1, width=10)
        self.min_bits_box.grid(row=1, column=1, sticky="w", pady=3)
        self.save_button = ttk.Button(settings, text="Save settings", command=self.save_settings)
        self.save_button.grid(row=0, column=2, rowspan=2, padx=(10, 0))
        self.settings_hint = ttk.Label(settings, style="Detail.TLabel")
        self.settings_hint.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.channel_entry.bind("<Return>", lambda _: self.save_settings())
        self.min_bits_box.bind("<Return>", lambda _: self.save_settings())

        bottom = ttk.Frame(self.root, padding=(16, 0, 16, 14))
        bottom.grid(row=1, column=0, sticky="nsew")
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(1, weight=1)
        ttk.Label(bottom, text="Log").grid(row=0, column=0, sticky="w", pady=(0, 4))
        log_frame = ttk.Frame(bottom)
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_box = tk.Text(log_frame, height=10, wrap="word", font=("Consolas", 10), state="disabled",
                               relief="flat", borderwidth=0, highlightthickness=1, padx=8, pady=6)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scrollbar.set)
        self.log_box.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

    def apply_theme(self, name: str):
        self.theme = name
        p = self.palette = THEMES[name]
        style_window(self.style, self.root, p)
        self.root.configure(bg=p["bg"])
        self.log_box.configure(bg=p["surface"], fg=p["fg"], insertbackground=p["fg"], selectbackground=p["select"],
                               selectforeground=p["fg"], highlightbackground=p["border"], highlightcolor=p["border"])
        self.log_box.tag_configure("warning", foreground=p["warning"])
        self.log_box.tag_configure("error", foreground=p["error"])
        self.theme_button.configure(text="☀  Light" if name == "dark" else "☾  Dark")
        set_title_bar_dark(self.root, name == "dark")

    def toggle_theme(self):
        self.apply_theme("light" if self.theme == "dark" else "dark")
        save_theme(self.theme)

    # ------------------------------------------------------------------ periodic update

    def _poll(self):
        try:  # one failing callback must not stop the window from updating
            while True:
                try:
                    finished = self.ui_calls.get_nowait()
                except queue.Empty:
                    break
                try:
                    finished()
                except Exception:
                    log.exception("Window error")
            self._drain_log()
            self._update_view()
            if self._close_when_stopped and self.state == "stopped":
                self.root.destroy()
                return
        except Exception:
            log.exception("Window error")
        self.root.after(POLL_MS, self._poll)

    def in_background(self, work, done):
        """Run work() off the window's thread (it may wait on Google), then done(result, error) back on it."""
        def run():
            try:
                result, error = work(), None
            except Exception as exc:
                result, error = None, exc
            self.ui_calls.put(lambda: done(result, error))
        threading.Thread(target=run, daemon=True).start()

    def _drain_log(self):
        items = []
        while True:
            try:
                items.append(self.lines.get_nowait())
            except queue.Empty:
                break
        if not items:
            return
        at_bottom = self.log_box.yview()[1] >= 0.999
        self.log_box.configure(state="normal")
        for level, text in items:
            tag = "error" if level >= logging.ERROR else "warning" if level >= logging.WARNING else ""
            self.log_box.insert("end", text + "\n", tag)
        extra = int(self.log_box.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if extra > 0:
            self.log_box.delete("1.0", f"{extra + 1}.0")
        self.log_box.configure(state="disabled")
        if at_bottom:
            self.log_box.see("end")

    def _update_view(self):
        state, tracker = self.state, self.tracker
        if state == "running" and tracker is not None:
            chat = {"connected": "chat connected", "connecting": "connecting to chat ..."}.get(tracker.chat_state, tracker.chat_state)
            headline = f"Tracking #{tracker.cfg['channel']}"
            parts = [chat]
            if tracker.streamlabs_state != "off":
                parts.append({"connected": "Streamlabs connected", "connecting": "connecting to Streamlabs ...",
                              "refused": "Streamlabs refused the token"}
                             .get(tracker.streamlabs_state, f"Streamlabs {tracker.streamlabs_state}"))
            if tracker.kofi_state != "off":
                parts.append({"ok": "Ko-fi tab checked", "starting": "checking the Ko-fi tab ...",
                              "error": "Ko-fi tab unreachable"}.get(tracker.kofi_state, f"Ko-fi {tracker.kofi_state}"))
            parts.append(f"events waiting for the sheet: {tracker.pending()}")
            detail = "   ·   ".join(parts)
        elif state == "starting":
            headline, detail = "Starting ...", "opening the Google Sheet"
        elif state == "stopping":
            headline, detail = "Stopping ...", "writing the last events to the sheet"
        else:
            headline, detail = "Stopped", "not listening to chat"
        self._set(self.headline, text=headline)
        self._set(self.detail, text=detail)

        self._set(self.start_button, text="Stop tracking" if state in ("starting", "running") else "Start tracking",
                  state="disabled" if state == "stopping" else "normal")
        self._set(self.refresh_button, state="normal" if state == "running" else "disabled")
        self._set(self.delete_button, state="normal" if state in ("stopped", "running") else "disabled")
        editable = "normal" if state == "stopped" else "disabled"
        for widget in (self.channel_entry, self.min_bits_box, self.save_button):
            self._set(widget, state=editable)
        self._set(self.settings_hint, text=(
            "Cheers under the minimum go to column I; the rest go to Bits (column E)."
            if state == "stopped" else "Stop tracking to change the settings."))

    @staticmethod
    def _set(widget, **options):
        """Configure only what changed, so the window does not flicker."""
        changed = {key: value for key, value in options.items() if str(widget.cget(key)) != str(value)}
        if changed:
            widget.configure(**changed)

    # ------------------------------------------------------------------ buttons

    def toggle(self):
        with self._lock:
            state = self.state
            if state == "starting":  # Stop pressed while the sheet is still opening
                self.tracker.request_stop()
                self.state = "stopping"
                log.info("Stopping ...")
                return
        if state == "stopped":
            self.start()
        elif state == "running":
            self.stop()

    def start(self):
        if not self.save_settings():
            return
        try:
            cfg = core.load_config()
        except SystemExit as exc:  # load_config explains config.json problems this way
            log.error("%s", exc.code)
            return
        self.tracker = core.Tracker(cfg)
        self.state = "starting"
        log.info("Starting: opening the Google Sheet ...")
        threading.Thread(target=self._start_worker, args=(self.tracker,), daemon=True).start()

    def _start_worker(self, tracker):
        try:
            started = tracker.start()
        except core.SheetError as exc:
            log.error("%s", exc)
            started = False
        except Exception:
            log.exception("Could not start tracking")
            started = False
        with self._lock:
            if started and not tracker.stopping.is_set():
                self.state = "running"
                return
        tracker.stop()  # also covers Stop pressed while the sheet was opening
        self.state = "stopped"

    def stop(self):
        self.state = "stopping"
        log.info("Stopping ...")
        threading.Thread(target=self._stop_worker, args=(self.tracker,), daemon=True).start()

    def _stop_worker(self, tracker):
        try:
            tracker.stop()
        except Exception:
            log.exception("Problem while stopping")
        self.state = "stopped"

    def refresh(self):
        if self.state == "running" and self.tracker is not None:
            self.tracker.refresh()

    def open_sheet(self):
        try:
            sheet_id = self._read_config().get("spreadsheet_id", "")
        except (OSError, ValueError) as exc:
            log.error("Could not read config.json: %s", exc)
            return
        if not sheet_id:
            log.error("No sheet has been saved yet. Paste its link under Sheet config and press Save.")
            return
        webbrowser.open(core.sheet_url(sheet_id))
        log.info("Opened the sheet in your web browser.")

    def open_sheet_dialog(self):
        if self.sheet_dialog is None or not self.sheet_dialog.win.winfo_exists():
            try:
                self.sheet_dialog = SheetDialog(self)
            except (OSError, ValueError) as exc:
                log.error("Could not read config.json: %s", exc)
                return
        self.sheet_dialog.win.lift()

    def open_delete_dialog(self):
        if self.delete_dialog is None or not self.delete_dialog.win.winfo_exists():
            self.delete_dialog = DeleteDialog(self)
        self.delete_dialog.win.lift()

    def editing_syncer(self):
        """The sheet writer to delete rows with. While tracking it is the tracker's own, so a deletion
        never lands in the middle of a sheet update. Runs in the background: it may wait on Google."""
        tracker = self.tracker
        if self.state == "running" and tracker is not None and tracker.syncer is not None:
            return tracker.syncer
        if self.state != "stopped":
            raise core.SheetError("Wait until tracking has finished starting or stopping, then try again.")
        try:
            cfg = core.load_config()
        except SystemExit as exc:
            raise core.SheetError(str(exc.code))
        return core.Syncer(core.Store(None), core.GoogleSheet(cfg), cfg)

    # ------------------------------------------------------------------ settings

    @staticmethod
    def _read_config() -> dict:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))

    @staticmethod
    def _write_config(cfg: dict):
        tmp = CONFIG_PATH.with_name("config.json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, CONFIG_PATH)

    def _load_settings(self):
        try:
            cfg = self._read_config()
        except (OSError, ValueError) as exc:
            log.error("Could not read config.json: %s", exc)
            return
        self.channel_var.set(cfg.get("channel", ""))
        self.min_bits_var.set(str(cfg.get("min_cheer_bits", core.DEFAULT_CONFIG["min_cheer_bits"])))

    def save_settings(self) -> bool:
        """Write the two settings to config.json if they changed. Returns False if they are not valid."""
        channel = self.channel_var.get().strip()
        min_bits = core.to_int(self.min_bits_var.get().strip(), 0)
        if not core.clean_channel(channel):
            messagebox.showerror(APP_TITLE, "Type the streamer's Twitch channel name.", parent=self.root)
            return False
        if min_bits < 1:
            messagebox.showerror(APP_TITLE, "The cheer minimum must be a whole number of bits, 1 or more.", parent=self.root)
            return False
        try:
            cfg = self._read_config()
        except (OSError, ValueError) as exc:
            log.error("Could not read config.json: %s", exc)
            return False
        if cfg.get("channel") == channel and cfg.get("min_cheer_bits") == min_bits:
            return True
        cfg["channel"], cfg["min_cheer_bits"] = channel, min_bits
        self._write_config(cfg)
        self.min_bits_var.set(str(min_bits))
        log.info("Settings saved: channel %s; cheers under %d bits go to column I.", channel, min_bits)
        return True

    # ------------------------------------------------------------------ closing

    def on_close(self):
        if self.delete_dialog is not None and self.delete_dialog.busy and self.delete_dialog.win.winfo_exists():
            return  # a deletion is being written; closing now could leave it half done
        if self.state == "stopped":
            self.root.destroy()
            return
        if not messagebox.askyesno(APP_TITLE, "Tracking is running. Stop tracking and close the window?", parent=self.root):
            return
        self._close_when_stopped = True
        if self.state in ("starting", "running"):
            self.toggle()


# --------------------------------------------------------------------------- popups

def make_dialog(app: App, title: str, on_close):
    """A themed popup over the main window, hidden until show_dialog()."""
    win = tk.Toplevel(app.root)
    win.withdraw()
    win.title(title)
    win.transient(app.root)
    win.resizable(False, False)
    win.configure(bg=app.palette["bg"])
    win.protocol("WM_DELETE_WINDOW", on_close)
    win.bind("<Escape>", lambda _: on_close())
    frame = ttk.Frame(win, padding=(20, 18))
    frame.grid(row=0, column=0, sticky="nsew")
    frame.columnconfigure(0, weight=1)
    return win, frame


def show_dialog(app: App, win, focus):
    """Centre the popup over the main window and show it; the main window waits until it closes."""
    win.update_idletasks()
    x = app.root.winfo_rootx() + (app.root.winfo_width() - win.winfo_reqwidth()) // 2
    y = app.root.winfo_rooty() + (app.root.winfo_height() - win.winfo_reqheight()) // 3
    win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    win.deiconify()
    set_title_bar_dark(win, app.theme == "dark")
    win.grab_set()
    focus.focus_set()


def show_number(value) -> str:
    if value in ("", None):
        return "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class DeleteDialog:
    """Choose a row by number or username, confirm, then empty it and move the rows below up."""

    PLACEHOLDER = "Please choose what to delete"
    BY_ROW = "Delete row number"
    BY_NAME = "Delete username"

    def __init__(self, app: App):
        self.app = app
        self.busy = False
        self.win, frame = make_dialog(app, "Delete a row", self.cancel)
        ttk.Label(frame, text="Delete a person's row", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, style="Detail.TLabel", wraplength=380, justify="left",
                  text="Empties the row, then moves every row below it up by one. "
                       "The Bags, Total and Total cheer formulas stay as they are.").grid(row=1, column=0, sticky="w", pady=(4, 14))

        self.choice = ttk.Combobox(frame, state="readonly", values=(self.BY_ROW, self.BY_NAME), width=38,
                                   style="Placeholder.TCombobox")
        self.choice.set(self.PLACEHOLDER)
        self.choice.grid(row=2, column=0, sticky="ew")
        self.choice.bind("<<ComboboxSelected>>", self._on_choice)

        self.value_label = ttk.Label(frame, text="Row number or username")
        self.value_label.grid(row=3, column=0, sticky="w", pady=(14, 4))
        self.value_var = tk.StringVar()
        self.value_entry = ttk.Entry(frame, textvariable=self.value_var, width=40, state="disabled")
        self.value_entry.grid(row=4, column=0, sticky="ew")
        self.value_entry.bind("<Return>", lambda _: self.delete())
        self.status = ttk.Label(frame, text="", style="Detail.TLabel")
        self.status.grid(row=5, column=0, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, sticky="e", pady=(16, 0))
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self.cancel)
        self.cancel_button.grid(row=0, column=0, padx=(0, 10))
        self.delete_button = ttk.Button(buttons, text="Delete", style="Danger.TButton", command=self.delete)
        self.delete_button.grid(row=0, column=1)
        show_dialog(app, self.win, self.choice)

    def _on_choice(self, _event=None):
        by_row = self.choice.get() == self.BY_ROW
        self.choice.configure(style="TCombobox")
        self.choice.selection_clear()
        self.value_label.configure(text="Row number (as shown in the sheet)" if by_row else "Username (not case sensitive)")
        self.value_entry.configure(state="normal")
        self.value_var.set("")
        self.status.configure(text="")
        self.value_entry.focus_set()

    def _set_busy(self, busy: bool, message: str = ""):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for widget in (self.cancel_button, self.delete_button, self.value_entry):
            widget.configure(state=state)
        self.choice.configure(state="disabled" if busy else "readonly")
        self.status.configure(text=message)

    def _error(self, message: str):
        self._set_busy(False)
        messagebox.showerror("Delete a row", message, parent=self.win)

    def cancel(self):
        if self.busy:
            return
        self.win.grab_release()
        self.win.destroy()

    def delete(self):
        if self.busy:
            return
        choice = self.choice.get()
        if choice not in (self.BY_ROW, self.BY_NAME):
            self._error("Choose what to delete first: a row number or a username.")
            return
        value = self.value_var.get().strip()
        by_row = choice == self.BY_ROW
        if not value:
            self._error("Type the row number to delete." if by_row else "Type the username to delete.")
            return
        if by_row and not value.isdigit():
            self._error(f"{value!r} is not a row number. Type digits only, for example 5.")
            return

        def find():
            syncer = self.app.editing_syncer()
            return syncer, syncer.find_row("row" if by_row else "name", value)

        self._set_busy(True, "Looking it up in the sheet ...")
        self.app.in_background(find, self._confirm)

    def _confirm(self, result, error):
        if not self.win.winfo_exists():
            return
        if error is not None:
            self._error(str(error))
            return
        syncer, found = result
        row, data = found["row"], found["data"]
        below = found["rows_below"]
        moves = ("No rows below it need to move." if not below else
                 "The row below it will move up one row." if below == 1 else
                 f"The {below} rows below it will move up one row.")
        message = (
            f"Delete row {row}?\n\n"
            f"Name:  {found['name'] or '(no name)'}\n"
            f"Tier 1: {show_number(data['B'])}    Tier 2: {show_number(data['C'])}    Tier 3: {show_number(data['D'])}\n"
            f"Bits: {show_number(data['E'])}    Dollars: {show_number(data['F'])}    <100 Bits cheers: {show_number(data['I'])}\n\n"
            f"{moves}\n\n"
            "The tracker cannot undo this. The sheet's own version history "
            "(File > Version history) can bring it back."
        )
        self._set_busy(False)
        if not messagebox.askyesno("Delete this row?", message, icon="warning", default="no", parent=self.win):
            self.status.configure(text="Nothing was deleted.")
            return
        tracker = self.app.tracker
        if self.app.state == "running" and tracker is not None and syncer is not tracker.syncer:
            self._error("Tracking started meanwhile. Nothing was deleted; please try again.")
            return

        self._set_busy(True, f"Deleting row {row} ...")
        self.app.in_background(lambda: syncer.delete_row(row, found["name"]),
                               lambda rows, err: self._deleted(row, found["name"], rows, err))

    def _deleted(self, row, name, rows, error):
        if error is not None:
            log.warning("Delete row %d failed: %s", row, error)
            if self.win.winfo_exists():
                self._error(str(error))
            return
        moved = rows[1:]
        log.info("Deleted row %d (%s). %s", row, name or "no name",
                 "No rows below it needed to move." if not moved else
                 f"Row {moved[0]} moved up one row." if len(moved) == 1 else
                 f"Rows {moved[0]}-{moved[-1]} moved up one row.")
        self.busy = False
        if self.win.winfo_exists():
            self.cancel()


def service_account_email(cfg: dict) -> str:
    path = Path(cfg.get("credentials_file", "service_account.json"))
    path = path if path.is_absolute() else core.BASE / path
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("client_email", "")
    except (OSError, ValueError):
        return ""


class SheetDialog:
    """Choose the Google Sheet (and the sheet, or tab, inside it) the tracker writes to."""

    def __init__(self, app: App):
        self.app = app
        self.busy = False
        saved = app._read_config()
        self.win, frame = make_dialog(app, "Sheet config", self.cancel)
        ttk.Label(frame, text="Sheet config", style="Title.TLabel").grid(row=0, column=0, sticky="w")

        email = service_account_email(saved)
        ttk.Label(frame, style="Detail.TLabel", wraplength=480, justify="left",
                  text="The Google Sheet the tracker writes to. Share it as Editor with the tracker's Google account:"
                       if email else "The Google Sheet the tracker writes to. The Google key (service_account.json) "
                                     "is missing, so the sheet cannot be checked yet.").grid(row=1, column=0, sticky="w", pady=(4, 4))
        if email:
            email_box = ttk.Entry(frame, width=62)
            email_box.insert(0, email)
            email_box.configure(state="readonly")
            email_box.grid(row=2, column=0, sticky="ew")

        ttk.Label(frame, text="Sheet URL").grid(row=3, column=0, sticky="w", pady=(16, 4))
        self.url_var = tk.StringVar(value=core.sheet_url(saved.get("spreadsheet_id", "")))
        self.url_entry = ttk.Entry(frame, textvariable=self.url_var, width=62)
        self.url_entry.grid(row=4, column=0, sticky="ew")
        ttk.Label(frame, text="Sheet name (the tab at the bottom of the spreadsheet)").grid(row=5, column=0, sticky="w", pady=(12, 4))
        self.name_var = tk.StringVar(value=saved.get("worksheet", core.DEFAULT_CONFIG["worksheet"]))
        self.name_entry = ttk.Entry(frame, textvariable=self.name_var, width=30)
        self.name_entry.grid(row=6, column=0, sticky="w")
        for entry in (self.url_entry, self.name_entry):
            entry.bind("<Return>", lambda _: self.save())
        self.status = ttk.Label(frame, text="", style="Detail.TLabel", wraplength=480, justify="left")
        self.status.grid(row=7, column=0, sticky="w", pady=(10, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=0, sticky="w", pady=(14, 0))
        self.save_button = ttk.Button(buttons, text="Save", style="Accent.TButton", command=self.save)
        self.save_button.grid(row=0, column=0, padx=(0, 10))
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self.cancel)
        self.cancel_button.grid(row=0, column=1, padx=(0, 10))
        ttk.Button(buttons, text="Open sheet", command=app.open_sheet).grid(row=0, column=2)

        if app.state != "stopped":
            for widget in (self.url_entry, self.name_entry, self.save_button):
                widget.configure(state="disabled")
            self.status.configure(text="Stop tracking to change the sheet. Open sheet still works.")
        show_dialog(app, self.win, self.url_entry)

    def _set_busy(self, busy: bool, message: str = ""):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for widget in (self.url_entry, self.name_entry, self.save_button, self.cancel_button):
            widget.configure(state=state)
        self.status.configure(text=message)

    def _error(self, message: str):
        self._set_busy(False)
        messagebox.showerror("Sheet config", message, parent=self.win)

    def cancel(self):
        if self.busy:
            return
        self.win.grab_release()
        self.win.destroy()

    def save(self):
        if self.busy or self.app.state != "stopped":
            return
        sheet_id = core.spreadsheet_id_from(self.url_var.get())
        tab = self.name_var.get().strip()
        if not sheet_id:
            self._error("That does not look like a Google Sheets link. Copy it from the browser's address bar "
                        "while the sheet is open; it contains /spreadsheets/d/.")
            return
        if not tab:
            self._error("Type the sheet name: the name of the tab at the bottom of the spreadsheet, like Points.")
            return
        try:
            saved = self.app._read_config()
        except (OSError, ValueError) as exc:
            self._error(f"Could not read config.json: {exc}")
            return
        if saved.get("spreadsheet_id") == sheet_id and saved.get("worksheet") == tab:
            self.status.configure(text="Nothing to save: this is already the tracker's sheet.")
            return
        cfg = {**core.DEFAULT_CONFIG, **saved, "spreadsheet_id": sheet_id, "worksheet": tab}

        def check():
            pending = len(core.Store(core.BASE / "data").pending())
            try:
                sheet = core.GoogleSheet(cfg)
                return {"title": sheet.title, "headers": sheet.headers(), "problem": None, "pending": pending}
            except core.SheetError as exc:
                return {"problem": str(exc), "pending": pending}
            except Exception as exc:
                return {"problem": f"Could not reach Google ({type(exc).__name__}: {exc})", "pending": pending}

        self._set_busy(True, "Checking the sheet ...")
        self.app.in_background(check, lambda result, error: self._confirm(saved, sheet_id, tab, result, error))

    def _confirm(self, saved, sheet_id, tab, result, error):
        if not self.win.winfo_exists():
            return
        self._set_busy(False)
        if error is not None:
            self._error(str(error))
            return
        problem = result["problem"]
        if problem is None:
            headers = " | ".join(str(h) for h in result["headers"][:10]) or "(empty)"
            lines = [f"Spreadsheet:  {result['title']}", f"Sheet name:  {tab}", f"Row 1:  {headers}", "",
                     "The tracker opened it and found that sheet."]
        else:
            lines = [f"Sheet ID:  {sheet_id}", f"Sheet name:  {tab}", "", "The tracker could not open it:", problem, "",
                     "You can still save it and fix this later; tracking will not start until it works."]
        if result["pending"]:
            lines += ["", f"{result['pending']} event(s) saved earlier are not in a sheet yet. "
                          "They will be written to this sheet the next time tracking starts."]
        message = "Save this as the tracker's sheet?\n\n" + "\n".join(lines)
        if not messagebox.askyesno("Save sheet config?", message, icon="warning" if problem else "question",
                                   default="no" if problem else "yes", parent=self.win):
            self.status.configure(text="Not saved.")
            return
        self.app._write_config({**saved, "spreadsheet_id": sheet_id, "worksheet": tab})
        self.url_var.set(core.sheet_url(sheet_id))
        self.status.configure(text="Saved. Tracking uses this sheet from the next Start.")
        log.info("Sheet config saved: %s, sheet %r.", result.get("title") or sheet_id, tab)


def main():
    try:  # sharp text on high-DPI screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    try:
        App(root)
    except Exception:
        messagebox.showerror(APP_TITLE, "The tracker window could not start:\n\n" + traceback.format_exc())
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
