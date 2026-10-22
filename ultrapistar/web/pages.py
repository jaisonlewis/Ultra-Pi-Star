"""HTML pages: phone remote, TV screen, host console."""
from __future__ import annotations

import base64
import io
import json
import time

import qrcode
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

bp = Blueprint("pages", __name__)


@bp.after_request
def _no_cache(resp):
    """Pages ship the JS/markup shell — they must never be cached or a
    phone can hold a UI missing features the API already supports."""
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _ctx():
    return current_app.extensions["ups"]


def qr_data_uri(text: str) -> str:
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def join_url() -> str:
    cfg = _ctx()["config"]
    return f"http://{cfg.lan_ip}:{cfg.port}/"


@bp.get("/")
def phone_home():
    """Mobile remote. Guests pick a display name, then search/queue."""
    ctx = _ctx()
    return render_template("phone.html",
                           join_url=join_url(),
                           host=False,
                           session_performer=session.get("performer_id"))


@bp.get("/tv")
def tv_screen():
    """Full-screen TV/kiosk view: idle/join screen + player + results."""
    # page-load liveness marker for the kiosk watchdog: if tv:ready doesn't
    # follow within PAGE_READY_S the renderer is wedged (loads HTML but
    # never runs the JS that connects the socket)
    _ctx()["tv_page_at"] = time.time()
    url = join_url()
    cfg = _ctx()["config"]
    return render_template("tv.html", join_url=url, qr=qr_data_uri(url),
                           host_key=cfg.host_key)


@bp.get("/host")
def host_console():
    """Host controls. Requires the startup host key (printed to console)."""
    cfg = _ctx()["config"]
    if request.args.get("key") == cfg.host_key:
        session["is_host"] = True
        return redirect(url_for("pages.host_console"))
    if not session.get("is_host"):
        return render_template("host_gate.html"), 403
    return render_template("host.html", join_url=join_url())


@bp.get("/media/<path:filename>")
def media(filename: str):
    """Serve local-provider media files."""
    from flask import send_from_directory
    path = _ctx()["config"].media_path(filename)
    return send_from_directory(path.parent, path.name)
