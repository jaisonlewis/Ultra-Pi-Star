"""Prep-station window: paste a song/playlist URL → GPU pipeline →
bundle lands on the prepared USB stick (or a folder), optionally pushed
to the Pi over the LAN."""
from __future__ import annotations

import queue
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, ttk

from . import gpu, settings
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

        self._prefs = settings.load()

        mid = ttk.Frame(self, padding=(8, 0))
        mid.pack(fill="x")
        ttk.Label(mid, text="Output:").pack(side="left")
        self.dest = tk.StringVar(
            value=self._prefs.get("dest") or self._default_dest())
        ttk.Entry(mid, textvariable=self.dest, width=46).pack(
            side="left", padx=6)
        ttk.Button(mid, text="Browse…", command=self.pick_dest).pack(
            side="left")
        ttk.Button(mid, text="Scan folder",
                   command=self.scan_folder).pack(side="left", padx=4)
        ttk.Button(mid, text="Organize folders",
                   command=self.organize_folder).pack(side="left")

        pirow = ttk.Frame(self, padding=(8, 4))
        pirow.pack(fill="x")
        self.push_var = tk.BooleanVar(
            value=bool(self._prefs.get("push")))
        ttk.Checkbutton(pirow, text="Push finished songs to Pi",
                        variable=self.push_var).pack(side="left")
        ttk.Label(pirow, text="http://").pack(side="left", padx=(12, 0))
        self.pi_host = ttk.Entry(pirow, width=20)
        self.pi_host.insert(
            0, self._prefs.get("pi_host", "utra-pi-star.local:5000"))
        self.pi_host.pack(side="left")
        ttk.Label(pirow, text="host key:").pack(side="left", padx=(8, 0))
        self.pi_key = ttk.Entry(pirow, width=8)
        self.pi_key.insert(0, self._prefs.get("pi_key", ""))
        self.pi_key.pack(side="left")

        tierrow = ttk.Frame(self, padding=(8, 0))
        tierrow.pack(fill="x")
        ttk.Label(tierrow, text="Models:").pack(side="left")
        self._tier_keys = list(gpu.TIERS)
        detected = gpu.detect_tier()
        want = self._prefs.get("tier") or detected
        self.tier = ttk.Combobox(
            tierrow, state="readonly", width=26,
            values=[f"{gpu.TIERS[k]['label']}" for k in self._tier_keys])
        self.tier.set(gpu.TIERS[want if want in gpu.TIERS
                                else detected]["label"])
        self.tier.pack(side="left", padx=6)
        gname = gpu.gpu_name()
        ttk.Label(
            tierrow,
            text=(f"detected: {gname} ({gpu.gpu_vram_gb():.0f} GB)"
                  if gname else "no GPU detected — CPU mode"),
            foreground="#666").pack(side="left")

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
        self.tree.bind("<Delete>", lambda _e: self.remove_selected())
        btn = ttk.Frame(self, padding=(8, 0))
        btn.pack(fill="x")
        ttk.Button(btn, text="Remove selected",
                   command=self.remove_selected).pack(side="left")
        ttk.Label(btn, text="Delete key works too — queued items drop out "
                            "instantly; a running song stops at its next "
                            "stage.",
                  foreground="#666").pack(side="left", padx=8)
        self.status = tk.StringVar(value="ready")
        ttk.Label(self, textvariable=self.status, anchor="w",
                  padding=8).pack(fill="x")
        self._rows: dict[int, str] = {}
        self.after(200, self._poll)
        self._ensure_station()   # resumes the persisted queue if any

    def _default_dest(self) -> str:
        drives = find_prepared_drives()
        return str(drives[0] if drives else REPO / "station-out")

    def pick_dest(self):
        d = filedialog.askdirectory(initialdir=self.dest.get() or ".")
        if d:
            self.dest.set(d)

    def _tier_key(self) -> str:
        label = self.tier.get()
        for k in self._tier_keys:
            if gpu.TIERS[k]["label"] == label:
                return k
        return gpu.detect_tier()

    def _save_prefs(self):
        settings.save({"dest": self.dest.get(), "push": self.push_var.get(),
                       "pi_host": self.pi_host.get().strip(),
                       "pi_key": self.pi_key.get().strip(),
                       "tier": self._tier_key()})

    def _ensure_station(self):
        self._save_prefs()
        if self._station is None:
            push = ({"url": "http://" + self.pi_host.get().strip(),
                     "key": self.pi_key.get().strip()}
                    if self.push_var.get() else None)
            self._station = Station(
                Path(self.dest.get()), REPO / "models",
                on_event=lambda *a: self._events.put(a), push=push,
                tier=self._tier_key())
            # surface jobs restored from the persisted queue
            for jid, job in self._station.jobs.items():
                self._events.put((jid, "queued", job["rec"]))
        elif gpu.current_tier() != self._tier_key():
            gpu.configure(self._tier_key())   # tier changed mid-run

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

    def scan_folder(self):
        """Process media already sitting in the output folder — e.g. a
        staging root pulled off the Pi (with pi-catalog.json) or a folder
        of local files. Anything already charted or bundled is skipped."""
        self._ensure_station()
        self.status.set("scanning output folder …")

        def work():
            try:
                added, skipped, adopted = self._station.add_folder(
                    Path(self.dest.get()))
                for jid, job in list(self._station.jobs.items()):
                    if job["status"] == "queued" and jid not in self._rows:
                        self._events.put((jid, "queued", job["rec"]))
                self._events.put((None, "summary",
                                  f"folder: {added} queued"
                                  + (f", {adopted} already done" if adopted
                                     else "")
                                  + (f", {skipped} skipped" if skipped
                                     else "")))
            except Exception as exc:
                self._events.put((None, "summary", f"scan error: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def organize_folder(self):
        """Reorganize the output folder into artist/title dirs — the
        one-shot migration for libraries written before the per-song
        folder layout."""
        self.status.set("organizing folders …")

        def work():
            try:
                from .organize import organize_root
                out = organize_root(Path(self.dest.get()))
                self._events.put(
                    (None, "summary",
                     f"organized {out['organized']} songs"
                     + (f", {out['unsorted']} unsorted" if out["unsorted"]
                        else "")))
            except Exception as exc:
                self._events.put((None, "summary",
                                  f"organize error: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def remove_selected(self):
        if not self._station:
            return
        inv = {iid: jid for jid, iid in self._rows.items()}
        n = 0
        for iid in self.tree.selection():
            jid = inv.get(iid)
            if jid is not None and self._station.remove(jid):
                n += 1
        if n:
            self.status.set(f"cancelled {n} job(s)")
        else:
            self.status.set("nothing to remove — select a queued row")

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
                elif ev == "cancelled":
                    vals[1], vals[2] = "", "cancelled"
                elif ev == "failed":
                    vals[2] = "failed"
                    self.status.set(f"{label}: {payload}")
                self.tree.item(self._rows[jid], values=vals)
        except queue.Empty:
            pass
        self.after(200, self._poll)


def main():
    App().mainloop()
