"""Small desktop window for the computer running the player: start, stop, restart, status.

    pythonw -m houselights control --config config.toml

The player normally runs as the "HouseLights Player" scheduled task (see
scripts/install-startup-task.ps1). Start runs that task, or the player directly
if the task isn't installed. Stop asks the player to black out and exit, and
only ends the task forcibly if it doesn't respond.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import ttk

from .config import load_config

TASK_NAME = "HouseLights Player"
POLL_MS = 2000
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flash from pythonw


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW)


def task_state() -> str | None:
    """'Running', 'Ready', 'Disabled'..., or None if the scheduled task isn't installed."""
    if os.name != "nt":
        return None
    r = _run("schtasks", "/Query", "/TN", TASK_NAME, "/FO", "CSV", "/NH")
    if r.returncode != 0:
        return None
    fields = [f.strip('"') for f in r.stdout.strip().splitlines()[-1].split('","')]
    return fields[-1].strip('"') if fields else None


class Controller:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.repo = self.config_path.parent
        cfg = load_config(self.config_path)
        self.port = cfg.http_port
        self.token = cfg.api_token
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = self.repo / "logs" / "player.log"

    def _request(self, path: str, body: dict | None = None, timeout: float = 2.0) -> dict:
        req = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET",
                                     data=None if body is None else json.dumps(body).encode())
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-Api-Token", self.token)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def status(self) -> dict | None:
        try:
            return self._request("/api/status")
        except OSError:
            return None

    def start(self) -> str:
        if self.status() is not None:
            return "Already running."
        if task_state() is not None:
            r = _run("schtasks", "/Run", "/TN", TASK_NAME)
            return "Starting…" if r.returncode == 0 else f"Couldn't start the task: {r.stderr.strip() or r.stdout.strip()}"
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pythonw if pythonw.exists() else sys.executable)
        self.log.parent.mkdir(exist_ok=True)
        subprocess.Popen(
            [exe, "-m", "houselights", "serve", "--config", str(self.config_path), "--log", str(self.log)],
            cwd=self.repo, creationflags=NO_WINDOW | 0x00000008,  # DETACHED_PROCESS
        )
        return "Starting (no scheduled task installed)…"

    def stop(self) -> str:
        try:
            self._request("/api/shutdown", {})
        except OSError:
            pass
        for _ in range(20):
            if self.status() is None:
                return "Stopped."
            threading.Event().wait(0.25)
        # It didn't shut down cleanly; force the scheduled task to end.
        if task_state() == "Running":
            _run("schtasks", "/End", "/TN", TASK_NAME)
            threading.Event().wait(1.0)
        return "Stopped (forced)." if self.status() is None else "Still running — check the log."

    def log_tail(self, lines: int = 12) -> str:
        try:
            with open(self.log, encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-lines:])
        except OSError:
            return "(no log yet)"


class App:
    def __init__(self, root: tk.Tk, ctl: Controller):
        self.root, self.ctl = root, ctl
        root.title("House Lights Player")
        root.minsize(460, 420)
        pad = {"padx": 12, "pady": 6}

        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        self.dot = tk.Canvas(top, width=16, height=16, highlightthickness=0)
        self.dot.pack(side="left")
        self.dot_id = self.dot.create_oval(2, 2, 14, 14, fill="#888", outline="")
        self.state_var = tk.StringVar(value="Checking…")
        ttk.Label(top, textvariable=self.state_var, font=("Segoe UI", 13, "bold")).pack(side="left", padx=8)

        self.detail_var = tk.StringVar()
        ttk.Label(root, textvariable=self.detail_var, justify="left").pack(anchor="w", **pad)

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", **pad)
        self.btn_start = ttk.Button(buttons, text="Start", command=lambda: self._do(ctl.start))
        self.btn_stop = ttk.Button(buttons, text="Stop", command=lambda: self._do(ctl.stop))
        self.btn_restart = ttk.Button(buttons, text="Restart", command=lambda: self._do(self._restart))
        for b in (self.btn_start, self.btn_stop, self.btn_restart):
            b.pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Open control page", command=lambda: webbrowser.open(f"http://localhost:{ctl.port}/")).pack(side="right")

        self.msg_var = tk.StringVar()
        ttk.Label(root, textvariable=self.msg_var, foreground="#a15c00").pack(anchor="w", padx=12)

        logbar = ttk.Frame(root)
        logbar.pack(fill="x", padx=12, pady=(10, 0))
        ttk.Label(logbar, text="Recent log").pack(side="left")
        ttk.Button(logbar, text="Open log", command=self._open_log).pack(side="right")
        self.log_text = tk.Text(root, height=12, wrap="none", font=("Consolas", 9), state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        self.busy = False
        self._poll()

    def _restart(self) -> str:
        self.ctl.stop()
        return self.ctl.start()

    def _do(self, action) -> None:
        if self.busy:
            return
        self.busy = True
        self.msg_var.set("Working…")
        for b in (self.btn_start, self.btn_stop, self.btn_restart):
            b.state(["disabled"])

        def work():
            try:
                msg = action()
            except Exception as e:  # show it rather than dying silently under pythonw
                msg = f"Error: {e}"
            self.root.after(0, lambda: self._done(msg))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, msg: str) -> None:
        self.busy = False
        self.msg_var.set(msg)
        self._refresh()

    def _open_log(self) -> None:
        if self.ctl.log.exists():
            os.startfile(self.ctl.log)  # type: ignore[attr-defined]
        else:
            self.msg_var.set("No log yet.")

    def _poll(self) -> None:
        threading.Thread(target=self._refresh_bg, daemon=True).start()
        self.root.after(POLL_MS, self._poll)

    def _refresh_bg(self) -> None:
        s, task, log = self.ctl.status(), task_state(), self.ctl.log_tail()
        self.root.after(0, lambda: self._show(s, task, log))

    def _refresh(self) -> None:
        self._poll_once = threading.Thread(target=self._refresh_bg, daemon=True)
        self._poll_once.start()

    def _show(self, s: dict | None, task: str | None, log: str) -> None:
        task_line = f"Startup task: {task or 'not installed'}"
        if s is None:
            self.state_var.set("Stopped")
            self.dot.itemconfigure(self.dot_id, fill="#888")
            self.detail_var.set(task_line)
        else:
            labels = {"show": "Running — show", "live": "Running — tablet override", "off": "Running — lights off",
                      "idle": "Running — waiting for show time"}
            self.state_var.set(labels.get(s["mode"], "Running"))
            color = {"show": "#2e9d5b", "live": "#e86f10", "off": "#666", "idle": "#3a78c2"}.get(s["mode"], "#2e9d5b")
            self.dot.itemconfigure(self.dot_id, fill=color)
            lines = []
            if s.get("playing"):
                lines.append(f"Playing: {s['playing']}  ({s['fps']} fps)")
            if s.get("in_window"):
                lines.append(f"Show on until {s['window_end']}")
            elif s.get("next_start"):
                lines.append(f"Next show: {datetime.fromisoformat(s['next_start']):%a %b %d, %I:%M %p}")
            else:
                lines.append("No upcoming show in the schedule")
            lines.append(task_line)
            self.detail_var.set("\n".join(lines))
        if not self.busy:
            self.btn_start.state(["!disabled"] if s is None else ["disabled"])
            self.btn_stop.state(["disabled"] if s is None else ["!disabled"])
            self.btn_restart.state(["disabled"] if s is None else ["!disabled"])
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", log)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def run(config_path: str | Path) -> None:
    root = tk.Tk()
    App(root, Controller(Path(config_path)))
    root.mainloop()
