#!/usr/bin/env python3
"""Ultra Pi Star — single-command entry point.

Starts the local service (phone remote + TV screen + host console) on the LAN.
Prefers eventlet for production serving (real WebSocket transport); falls
back to the threaded dev server when eventlet isn't installed or is
disabled via ULTRAPISTAR_NO_EVENTLET=1.
"""
import os

try:
    if os.environ.get("ULTRAPISTAR_NO_EVENTLET"):
        raise ImportError
    import eventlet
    # Sockets go green for the WSGI/SocketIO layer only — threads,
    # subprocesses and time stay real so the heavy media workers
    # (ffmpeg, ASR, RMVPE) can't freeze the event loop.
    eventlet.monkey_patch(socket=True, select=True)
    ASYNC_MODE = "eventlet"
except ImportError:
    ASYNC_MODE = "threading"

from ultrapistar.app import create_app


def main() -> None:
    app, socketio, config = create_app(async_mode=ASYNC_MODE)
    join_url = f"http://{config.lan_ip}:{config.port}/"
    print("=" * 60)
    print(" Ultra Pi Star")
    print(f"   TV screen : http://localhost:{config.port}/tv")
    print(f"   Phone join: {join_url}")
    print(f"   Host page : http://localhost:{config.port}/host?key={config.host_key}")
    print(f"   Host key  : {config.host_key}")
    print("=" * 60, flush=True)
    socketio.run(app, host="0.0.0.0", port=config.port,
                 allow_unsafe_werkzeug=ASYNC_MODE == "threading")


if __name__ == "__main__":
    main()
