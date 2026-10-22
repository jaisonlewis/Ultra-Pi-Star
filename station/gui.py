"""Prep-station window: paste a song/playlist URL → GPU pipeline →
bundle lands on the prepared USB stick (or a folder), optionally pushed
to the Pi over the LAN."""
from __future__ import annotations

import queue
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, ttk

from .bundle import find_prepared_drives
from .jobs import Station

REPO = Path(__file__).resolve().parent.parent


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Ultra Pi Star — Prep Station")
        self.minsize(720, 420)
        self._events: queue.Queue = queue.Queue()
        self._station = None

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="YouTube URL / playlist:").pack(side="left")
        self.url = ttk.Entry(top, width=52)
        self.url.pack(side="left", padx=6, fill="x", expand=True)
        self.url.bind("<Return>", lambda _e: self.add_url())
        ttk.Button(top, text="Add", command=self.add_url).pack(side="left")

        mid = ttk.Frame(self, padding=(8, 0))
        mid.pack(fill="x")
        ttk.Label(mid, text="Output:").pack(side="left")
        self.dest = tk.StringVar(value=self._default_dest())
        ttk.Entry(mid, textvariable=self.dest, width=46).pack(
            side="left", padx=6)
        ttk.Button(mid, text="Browse…", command=self.pick_dest).pack(
            side="left")

        pirow = ttk.Frame(self, padding=(8, 4))
        pirow.pack(fill="x")
        self.push_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(pirow, text="Push finished songs to Pi",
                        variable=self.push_var).pack(side="left")
        ttk.Label(pirow, text="http://").pack(side="left", padx=(12, 0))
        self.pi_host = ttk.Entry(pirow, width=20)
        self.pi_host.insert(0, "utra-pi-star.local:5000")
        self.pi_host.pack(side="left")
        ttk.Label(pirow, text="host key:").pack(side="left", padx=(8, 0))
        self.pi_key = ttk.Entry(pirow, width=8)
        self.pi_key.pack(side="left")

        cols = ("title", "stage", "status")
        self.tree = ttk.Treeview(self, columns=cols, show="headings",
                                 height=14)
        self.tree.heading("title", text="Song")
        self.tree.heading("stage", text="Stage")
        self.tree.heading("status", text="Status")
        self.tree.column("title", width=420)
        self.tree.column("stage", width=160)
        self.tree.column("status", width=120)
        self.tree.pack(fill="both", expand=True, padx=8, pady=6)
        self.status = tk.StringVar(value="ready")
        ttk.Label(self, textvariable=self.status, anchor="w",
                  padding=8).pack(fill="x")
        self._rows: dict[int, str] = {}
        self.after(200, self._poll)

    def _default_dest(self) -> str:
        drives = find_prepared_drives()
        return str(drives[0] if drives else REPO / "station-out")

    def pick_dest(self):
        d = filedialog.askdirectory(initialdir=self.dest.get() or ".")
        if d:
            self.dest.set(d)

    def _ensure_station(self):
        if self._station is None:
            push = ({"url": "http://" + self.pi_host.get().strip(),
                     "key": self.pi_key.get().strip()}
                    if self.push_var.get() else None)
            self._station = Station(
                Path(self.dest.get()), REPO / "models",
                on_event=lambda *a: self._events.put(a), push=push)

    def add_url(self):
        url = self.url.get().strip()
        if not url:
            return
        self.url.delete(0, "end")
        self._ensure_station()
        self.status.set(f"expanding {url} …")

        def work():
            try:
                title, ids, skipped = self._station.expand_and_add(url)
                for jid in ids:
                    self._events.put((jid, "queued",
                                      self._station.jobs[jid]["rec"]))
                self._events.put((None, "summary",
                                  f"{title}: {len(ids)} queued"
                                  + (f", {skipped} already done"
                                     if skipped else "")))
            except Exception as exc:
                self._events.put((None, "summary", f"error: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                jid, ev, payload = self._events.get_nowait()
                if jid is None:
                    self.status.set(str(payload))
                    continue
                job = self._station.jobs.get(jid) if self._station else {}
                rec = (job or {}).get("rec") or payload or {}
                label = rec.get("title") or rec.get("source_id") or str(jid)
                if jid not in self._rows:
                    self._rows[jid] = self.tree.insert(
                        "", "end", values=(label, "", "queued"))
                vals = list(self.tree.item(self._rows[jid], "values"))
                if ev == "stage":
                    vals[1] = payload
                elif ev == "done":
                    vals[1], vals[2] = "done", "ready"
                elif ev == "pushed":
                    vals[1], vals[2] = "pushed", "on Pi"
                elif ev == "failed":
                    vals[2] = "failed"
                    self.status.set(f"{label}: {payload}")
                self.tree.item(self._rows[jid], values=vals)
        except queue.Empty:
            pass
        self.after(200, self._poll)


def main():
    App().mainloop()
