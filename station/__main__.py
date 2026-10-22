"""python -m station — launch the prep-station window.

``python -m station --scan <folder> [--all] [--tier <tier>]`` runs
headless instead: enqueue every media file in the folder that needs
work (every file with ``--all`` — a quality re-process of charted
entries too) and run the GPU pipeline serially until the queue drains.
"""
import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _scan(argv):
    p = argparse.ArgumentParser(prog="python -m station")
    p.add_argument("--scan", metavar="FOLDER", required=True,
                   help="staging root holding media (+ pi-catalog.json)")
    p.add_argument("--all", action="store_true",
                   help="re-process entries that already have charts")
    p.add_argument("--tier", choices=None, default=None,
                   help="model tier override (cpu|low|mid|max)")
    args = p.parse_args(argv)

    from .jobs import Station
    from . import gpu
    if args.tier and args.tier in gpu.TIERS:
        gpu.configure(args.tier)
    root = Path(args.scan)
    t0 = time.time()
    done = [0]

    def on_event(jid, ev, payload):
        if ev == "stage":
            print(f"  [{jid}] {payload}", flush=True)
        elif ev == "done":
            done[0] += 1
            el = int(time.time() - t0)
            print(f"[{jid}] DONE {payload.get('artist','')} - "
                  f"{payload.get('title','')}  ({el//60}m elapsed)",
                  flush=True)
        elif ev == "failed":
            done[0] += 1
            print(f"[{jid}] FAILED {payload}", flush=True)

    st = Station(root, REPO / "models", on_event=on_event)
    added, skipped = st.add_folder(root, include_charted=args.all)
    print(f"queued {added} jobs ({skipped} skipped) — serial pipeline",
          flush=True)
    st._q.join()
    print(f"all jobs finished: {done[0]} processed", flush=True)


def main_cli():
    if len(sys.argv) > 1 and sys.argv[1] == "--scan":
        _scan(sys.argv[1:])
    else:
        from .gui import main
        main()


if __name__ == "__main__":
    main_cli()
