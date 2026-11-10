# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 D-LAB-5
"""
start.py — what the two start commands run. Owned by the edge layer.

    <tool>-start        this computer only           python -m <pkg>.edge.start --port N
    <tool>-start-lan    this computer and its network python -m <pkg>.edge.start --lan --port N

An edge already answering on the port is stopped first, so switching between
the two is running the other command; a restart from the page keeps the mode
it was started in. On the network, the page answers other computers; the
chat needs its key there (routes.CHAT_KEY), and adding, choosing or
forgetting a connection stays on this computer.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.request
from typing import List, Optional

from . import settings


def _port(args: List[str]) -> Optional[int]:
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            return int(args[i + 1])
        if a.startswith("--port="):
            return int(a.split("=", 1)[1])
    return None


def _answers(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2):
            return True
    except Exception:
        return False


def _stop(port: int) -> None:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
    for _ in range(20):
        if not _answers(port):
            return
        time.sleep(0.5)


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    lan = "--lan" in args
    args = [a for a in args if a != "--lan"]
    if lan and "--host" not in args and not any(a.startswith("--host=") for a in args):
        args += ["--host", "0.0.0.0"]
    port = _port(args)
    if port and _answers(port):
        print(f"  an edge is running on port {port}: stopping it, to start "
              + ("for the network" if lan else "for this computer only"), flush=True)
        _stop(port)
    cmd = [sys.executable, "-m", settings.PACKAGE, *args]
    if sys.platform == "win32":                    # execv on Windows does not replace the console's process
        import subprocess
        return subprocess.call(cmd)
    os.execv(sys.executable, cmd)
    return 0                                       # not reached


if __name__ == "__main__":
    raise SystemExit(main())
