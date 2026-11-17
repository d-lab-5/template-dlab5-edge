# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 D-LAB-5
"""
routes.py — the edge's HTTP API, as one Flask blueprint, and install(app),
the one call that wires the layer into a tool. Owned by the edge layer.

    install(app)          in app.py, after create_app: the blueprint, the
                          local-only guard, the job kinds
    start_background()    in main(), before serving: the token keeper and
                          first start (scripts/edge-wire.py adds both)

    GET  /api/cloud                     connections (never a token) and the pairing in progress
    POST /api/cloud/link                {cloud, profile}: start pairing; the page shows the code
    POST /api/cloud/unlink | /use       {profile}
    GET  /api/jobs, /api/jobs/<id>      progress
    POST /api/jobs/<id>/cancel
    GET  /api/knowledge                 the local A-Boxes
    POST /api/knowledge/sync            {graph?}: a job, every linked connection
    GET|POST /api/chat …                the chat panel (chat.py): ask, new, stop
    GET  /api/chat/history[/<session>]  the last ten conversations; POST …/resume continues one
    GET|POST /api/chat/proposals        what the chat proposed; POST …/<id>/confirm|dismiss

Started for the network (<tool>-start-lan, or --host 0.0.0.0), the page
answers other computers too. Adding, choosing or forgetting a connection
stays on this computer (HERE_ONLY); the chat needs its key there (CHAT_KEY).

    GET  /api/chat/key        here only: the key, and the links that carry it
    POST /api/chat/key        {key}: another computer gives it once, gets a cookie
    POST /api/chat/key/new    here only: a new key; every other computer is out
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, Flask, Response, abort, current_app, jsonify, request

from . import chat as chat_mod
from . import cloud, firststart, settings, sync
from . import knowledge as kn
from .jobs import JOBS, Busy, Stopped

bp = Blueprint("edge", __name__, static_folder="static", static_url_path="/edge-static")

LINK_LOCK = threading.Lock()
LINK: Dict[str, Any] = {"state": "idle"}
KEEP_FRESH_EVERY_S = 12 * 3600

# What a browser on another computer may not do, when the edge is started for
# the network: a connection holds this edge's device token. Shutdown, restart
# and update are refused by the core already.
HERE_ONLY = ("/api/cloud/link", "/api/cloud/unlink", "/api/cloud/use")
NOT_HERE = "only on the computer the edge runs on: open the edge there"

# The chat is a Claude session of this computer's user: their conversations,
# their subscription, their confirm button. Claude has no shell, files or web
# in it, only this edge's tools, and every change waits for a click; still,
# not everyone on the network gets it. Another computer gives the CHAT KEY
# once (shown on this computer's page, never printed to the log) and keeps a
# cookie derived from it; a new key signs every other computer out. What the
# MCP server posts (new proposals) and the key itself stay here.
CHAT = "/api/chat"
CHAT_HERE_ONLY = {("GET", "/api/chat/key"), ("POST", "/api/chat/key/new"),
                  ("GET", "/api/chat/proposals"), ("POST", "/api/chat/proposals")}   # the MCP server's
CHAT_OPEN = {("GET", "/api/chat"), ("POST", "/api/chat/key")}                        # say why / give the key
CHAT_COOKIE = "edge-chat"
CHAT_COOKIE_DAYS = 30


def here() -> bool:
    """The caller is this computer (loopback), not another one on the network."""
    try:
        return ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        return False


def on_network() -> bool:
    """Started for the network: the core lets other names through then."""
    return bool(current_app.config.get("CORE_EXTRA_HOSTS"))


def chat_on_network() -> bool:
    """settings.CHAT_ON_NETWORK = False keeps the chat on this computer."""
    return on_network() and getattr(settings, "CHAT_ON_NETWORK", True) is not False


def _key_file() -> Path:
    return kn.data_dir() / "chat-key"


def chat_key(new: bool = False) -> str:
    """The chat key, made on first use, owner-only like the other state."""
    path = _key_file()
    if not new:
        try:
            key = path.read_text(encoding="utf-8").strip()
            if len(key) >= 20:
                return key
        except OSError:
            pass
    key = secrets.token_urlsafe(18)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    os.replace(tmp, path)
    return key


def _cookie_for(key: str) -> str:
    # not the key itself: a cookie read off a machine does not give the key away
    return hmac.new(key.encode(), b"edge chat", hashlib.sha256).hexdigest()


def chat_allowed() -> bool:
    """This computer always; another one with the cookie, when allowed at all."""
    if here():
        return True
    if not chat_on_network():
        return False
    got = request.cookies.get(CHAT_COOKIE, "")
    return bool(got) and hmac.compare_digest(got, _cookie_for(chat_key()))


def body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "send a JSON object")
    return data


def submit(kind: str, spec: Dict[str, Any]):
    try:
        return jsonify(JOBS.submit(kind, spec)), 202
    except Busy as exc:
        abort(409, str(exc))


# -------------------------------------------------------------- connections --

@bp.get("/api/cloud")
def cloud_state():
    with LINK_LOCK:
        link = {k: v for k, v in LINK.items() if k != "pending"}
        pending = LINK.get("pending")
        if pending and here():
            link.update(user_code=pending.get("user_code"), url=pending.get("verification_uri_complete"),
                        profile=pending.get("profile"))
    return jsonify({**cloud.public_state(), "profiles": cloud.profiles(), "link": link, "here": here()})


def _poll(pending: Dict[str, Any]) -> None:
    def waiting(status: str) -> None:
        with LINK_LOCK:
            LINK["status"] = status
    try:
        state = cloud.poll_link(pending, cancelled=lambda: LINK.get("pending") is not pending, on_wait=waiting)
        with LINK_LOCK:
            LINK.clear()
            LINK.update(state="linked", profile=state.get("profile"))
        start_first_start()                        # just linked: fetch what this edge lacks
    except cloud.CloudError as exc:
        with LINK_LOCK:
            if LINK.get("pending") is pending:
                LINK.clear()
                LINK.update(state="failed", error=str(exc))


@bp.post("/api/cloud/link")
def cloud_link():
    data = body()
    base = str(data.get("cloud") or "").strip()
    if not base:
        abort(400, f"say which cloud: the edge API address of {settings.CLOUD_NAME} (its About page shows it)")
    try:
        cloud.PROFILE = cloud.check_profile(str(data.get("profile") or cloud.active_profile()))
        pending = cloud.start_link(base)
    except cloud.CloudError as exc:
        abort(400, str(exc))
    with LINK_LOCK:
        LINK.clear()
        LINK.update(state="waiting", pending=pending)
    threading.Thread(target=_poll, args=(pending,), name="link-poll", daemon=True).start()
    return jsonify({"user_code": pending["user_code"], "url": pending["verification_uri_complete"],
                    "expires_in": pending["expires_in"]})


@bp.post("/api/cloud/unlink")
def cloud_unlink():
    """Forget one connection's token here. An admin revokes the edge itself
    in that cloud's Settings → Edges."""
    name = (request.get_json(silent=True) or {}).get("profile") or None
    with LINK_LOCK:
        pending = LINK.get("pending")
        if not name or (pending and pending.get("profile") == name):
            LINK.clear()
            LINK["state"] = "idle"
    try:
        cloud.forget(cloud.check_profile(name) if name else None)
    except cloud.CloudError as exc:
        abort(400, str(exc))
    return jsonify(cloud.public_state())


@bp.post("/api/cloud/use")
def cloud_use():
    try:
        cloud.use_profile(str(body().get("profile") or ""))
        cloud.PROFILE = None
    except cloud.CloudError as exc:
        abort(400, str(exc))
    return jsonify(cloud.public_state())


# --------------------------------------------------------------------- jobs --

@bp.get("/api/jobs")
def jobs_list():
    return jsonify(JOBS.list())


@bp.get("/api/jobs/<job_id>")
def job_get(job_id: str):
    try:
        return jsonify(JOBS.get(job_id))
    except KeyError:
        abort(404, f"no job {job_id}")


@bp.post("/api/jobs/<job_id>/cancel")
def job_cancel(job_id: str):
    try:
        return jsonify(JOBS.cancel(job_id))
    except KeyError:
        abort(404, f"no job {job_id}")


# ---------------------------------------------------------------- knowledge --

@bp.get("/api/knowledge")
def knowledge_list():
    kb = kn.KnowledgeBase()
    return jsonify({"graphs": [{"name": g, "statements": len(kb.load(g))} for g in kb.graphs()]})


def _sync_job(spec: Dict[str, Any], progress, cancelled) -> Dict[str, Any]:
    kb = kn.KnowledgeBase()
    graphs = [spec["graph"]] if spec.get("graph") else kb.graphs()
    out = {}
    for n, g in enumerate(graphs):
        if cancelled():
            raise Stopped()
        progress(f"{g}", n / max(1, len(graphs)))
        out[g] = sync.sync_everywhere(kb, g, spec.get("resolve"))
    return out


@bp.post("/api/knowledge/sync")
def knowledge_sync():
    if not cloud.linked_profiles():
        abort(409, "no connection is linked")
    data = request.get_json(silent=True) or {}
    spec = {k: data[k] for k in ("graph", "resolve") if data.get(k)}
    if spec.get("graph") and not kn.GRAPH_NAME.fullmatch(str(spec["graph"])):
        abort(400, "graph is a space's slug: lowercase letters, digits and -")
    return submit("knowledge-sync", spec)


# --------------------------------------------------------------------- chat --

_CHAT: Dict[str, Any] = {}


def chat_state() -> Tuple[chat_mod.Proposals, chat_mod.Chat]:
    if not _CHAT:
        # waiting proposals and the history live in the edge's data folder;
        # Claude Code's own session goes to a private folder made for this run
        # (0700, a name nobody can guess or make first): outside the home
        # folder, so no other project's CLAUDE.md is read
        _CHAT["proposals"] = chat_mod.Proposals(kn.data_dir() / "chat-proposals.json")
        _CHAT["chat"] = chat_mod.Chat(Path(tempfile.mkdtemp(prefix=f"{settings.TOOL}-chat-")))
        _CHAT["history"] = chat_mod.History(kn.data_dir() / "chat-history.json")
    return _CHAT["proposals"], _CHAT["chat"]


def chat_history() -> chat_mod.History:
    chat_state()
    return _CHAT["history"]


def chat_selection() -> Optional[str]:
    """What the person has chosen on the tool's page, if the tool has such a
    thing: settings.CHAT_SELECTION, a function returning a string that changes
    when the selection does (or None). The tool's own MCP tool reads the items."""
    get = getattr(settings, "CHAT_SELECTION", None)
    try:
        return get() if callable(get) else None
    except Exception:                               # a tool's hook must not break the chat
        return None


@bp.get("/api/chat")
def chat_status():
    if not chat_allowed():
        if chat_on_network():
            return jsonify({"available": False, "here": False, "needs_key": True,
                            "why": "The chat needs its key: on the computer the edge runs on, the chat panel shows it."})
        return jsonify({"available": False, "here": False, "why": f"The chat is {NOT_HERE}."})
    proposals, talk = chat_state()
    return jsonify({**chat_mod.available(), "here": here(), "network": chat_on_network() and here(),
                    "session": bool(talk.session), "answering": talk.proc is not None,
                    "waiting": [p for p in proposals.items.values() if p["state"] == "waiting"]})


@bp.post("/api/chat")
def chat_ask():
    """Ask Claude; the answer streams back as one JSON event per line."""
    data = body()
    message = str(data.get("message") or "").strip()
    if not message or len(message) > 8000:
        abort(400, "message must be 1 to 8000 characters")
    if not chat_mod.available()["available"]:
        abort(503, "Claude Code is not installed on this computer")
    lang = data.get("lang") if data.get("lang") in ("en", "de", "fr") else "en"
    port = int(request.host.rsplit(":", 1)[-1]) if ":" in request.host else 80
    proposals, talk = chat_state()
    selection = chat_selection()

    def stream():
        told = proposals.news()                    # what the person confirmed or dismissed meanwhile
        for ev in talk.ask(told + message, port, lang, selection):
            if ev["t"] == "proposal":
                p = proposals.items.get(ev["id"])
                if p:
                    ev = {**ev, "summary": p["summary"], "state": p["state"]}
            elif ev["t"] == "done" and talk.session:
                try:                               # into the history, so it outlives a restart
                    chat_history().record(talk.session, chat_mod.transcript_path(talk.workdir, talk.session))
                except (OSError, ValueError):
                    pass
            yield json.dumps(ev) + "\n"
    return Response(stream(), mimetype="application/x-ndjson", headers={"Cache-Control": "no-store"})


@bp.get("/api/chat/key")
def chat_key_show():
    """Here only: the key, and links to this edge that carry it in the
    fragment (#chat-key=…), which a browser never sends to a server."""
    if not chat_on_network():
        return jsonify({"network": False})
    key = chat_key()
    port = request.host.rsplit(":", 1)[-1] if ":" in request.host else "80"
    names = sorted(current_app.config.get("CORE_EXTRA_HOSTS") or (),
                   key=lambda n: (not n[0].isdigit(), n.startswith("127."), n))
    links = [f"http://{n}:{port}/#chat-key={key}" for n in names if not n.startswith("127.")]
    return jsonify({"network": True, "key": key, "links": links[:3]})


@bp.post("/api/chat/key")
def chat_key_give():
    """Another computer gives the key once and keeps a cookie for a month."""
    if not chat_on_network():
        abort(403, f"the chat is {NOT_HERE}")
    given = str(body().get("key") or "").strip()
    key = chat_key()
    if not given or not hmac.compare_digest(given, key):
        time.sleep(1)                              # no guessing at speed
        abort(403, "that is not this edge's chat key")
    resp = jsonify({"ok": True})
    resp.set_cookie(CHAT_COOKIE, _cookie_for(key), max_age=CHAT_COOKIE_DAYS * 86400,
                    httponly=True, samesite="Strict", path="/")
    return resp


@bp.post("/api/chat/key/new")
def chat_key_new():
    """Here only: a new key. Every other computer has to give it again."""
    chat_key(new=True)
    return chat_key_show()


@bp.post("/api/chat/new")
def chat_new():
    chat_state()[1].new()
    return jsonify({"ok": True})


@bp.post("/api/chat/stop")
def chat_stop():
    chat_state()[1].stop()
    return jsonify({"ok": True})


@bp.get("/api/chat/history")
def chat_history_list():
    """The last ten conversations, newest first."""
    talk = chat_state()[1]
    return jsonify({"items": chat_history().items(), "current": talk.session})


@bp.get("/api/chat/history/<session>")
def chat_history_one(session: str):
    try:
        return jsonify(chat_history().get(session))
    except KeyError:
        abort(404, "no such conversation in the history")


@bp.post("/api/chat/history/<session>/resume")
def chat_history_resume(session: str):
    """Continue an earlier conversation: Claude remembers what was said."""
    talk = chat_state()[1]
    if talk.proc is not None:
        abort(409, "Claude is still answering; stop it first")
    try:
        return jsonify(chat_history().resume(talk, session))
    except KeyError:
        abort(404, "no such conversation in the history")


@bp.get("/api/chat/proposals")
def chat_proposals():
    """Every proposal of this run and what became of it (the proposals() tool)."""
    return jsonify(chat_state()[0].listing())


@bp.post("/api/chat/proposals")
def chat_propose():
    """From the MCP server in panel mode: a change to wait for the person."""
    data = body()
    try:
        p = chat_state()[0].add(data.get("summary", ""), data.get("method", ""), data.get("path", ""),
                                data.get("body") or {})
    except ValueError as exc:
        abort(400, str(exc))
    return jsonify(p), 201


@bp.post("/api/chat/proposals/<pid>/<what>")
def chat_decide(pid: str, what: str):
    """The person's click: confirm runs the proposed call, dismiss drops it."""
    from flask import current_app
    if what not in ("confirm", "dismiss"):
        abort(404)
    proposals = chat_state()[0]
    waiting = proposals.items.get(pid)
    if what == "confirm" and waiting and not here() \
            and any(waiting["path"] == h or waiting["path"].startswith(h + "/") for h in HERE_ONLY):
        abort(403, f"confirm this one on the computer the edge runs on: it is {NOT_HERE}")
    try:
        p = proposals.take(pid, "confirmed" if what == "confirm" else "dismissed")
    except KeyError:
        abort(404, "this proposal is gone: the edge was restarted since it was made. Ask the chat to propose it again.")
    except ValueError as exc:
        abort(409, str(exc))
    if what == "dismiss":
        return jsonify({"state": "dismissed"})
    with current_app.test_client() as c:
        r = c.open(p["path"], method=p["method"], json=p["body"], headers={"Host": request.host})
    result = r.get_json(silent=True)
    problem = None if r.status_code < 400 else _problem(result, r.status_code)
    proposals.ran(pid, problem is None, problem)
    return jsonify({"state": "confirmed", "status": r.status_code, "result": result, "problem": problem}), \
        (200 if r.status_code < 400 else 502)


def _problem(result: Any, status: int) -> str:
    """Why a confirmed proposal failed, in a line the chat and the page can show."""
    if isinstance(result, dict):
        if result.get("errors"):
            return "; ".join(map(str, result["errors"]))
        if result.get("description") or result.get("error"):
            return str(result.get("description") or result.get("error"))
    return f"HTTP {status}"


# ------------------------------------------------------------------ bridges --
# Only the bridge of this edge's kind exists (copier writes one or none).

try:
    from .bridges import nodered
except ImportError:
    nodered = None
try:
    from .bridges import ros2
except ImportError:
    ros2 = None

if nodered is not None:
    @bp.get("/api/nodered")
    def nodered_status():
        out = nodered.available()
        if out["available"]:
            out["flows"] = nodered.flows()
        return jsonify(out)

    @bp.post("/api/nodered/call")
    def nodered_call():
        data = body()
        try:
            return jsonify(nodered.call(str(data.get("path") or ""), data.get("body") or {}))
        except ValueError as exc:
            abort(400, str(exc))
        except OSError as exc:
            abort(502, f"Node-RED did not answer: {exc}")

if ros2 is not None:
    @bp.get("/api/ros2")
    def ros2_status():
        out = ros2.available()
        if out["available"]:
            out["topics"] = ros2.topics()
        return jsonify(out)

    @bp.post("/api/ros2/publish")
    def ros2_publish():
        data = body()
        if not ros2.available()["available"]:
            abort(503, "ROS 2 is not available to this edge")
        return jsonify(ros2.publish(str(data.get("topic") or ""), str(data.get("type") or ""), data.get("data") or {}))


# ---------------------------------------------------------------- the guard --

def _guard(app: Flask) -> None:
    """This server answers to this machine only.

    DNS rebinding: a web page can make its own name resolve to 127.0.0.1 and
    then talk to this server as if it were the page's own origin. Binding to
    loopback does not stop that; checking the Host does. A write must also
    come from this page (or carry no Origin: an agent, a script). And no
    other page may frame this one.
    """
    extra = {h.lower() for h in os.environ.get(f"{settings.ENV}_EXTRA_HOSTS", "").split(",") if h}

    @app.before_request
    def _this_machine_only():
        host = (request.host or "").lower()
        name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
        if name not in {"127.0.0.1", "localhost", "[::1]"} | extra | set(app.config.get("CORE_EXTRA_HOSTS", ())):
            abort(403, "this server answers to localhost only")
        under = lambda ps: any(request.path == p or request.path.startswith(p + "/") for p in ps)  # noqa: E731
        if not here():
            asked = (request.method, request.path.rstrip("/") or "/")
            if under(HERE_ONLY) or asked in CHAT_HERE_ONLY:
                abort(403, f"this is {NOT_HERE}")
            if under((CHAT,)) and asked not in CHAT_OPEN and not chat_allowed():
                abort(403, "the chat needs its key: the chat panel on the computer the edge runs on shows it")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/").lower() != f"{request.scheme}://{host}":
                abort(403, "requests from another site are refused")

    @app.after_request
    def _no_framing(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp


# ---------------------------------------------------------- background work --

def start_first_start() -> None:
    """The first-start job, when anything is still missing."""
    try:
        if firststart.needed():
            JOBS.submit("first-start", {})
    except (Busy, cloud.CloudError):
        pass


def _keep_tokens_fresh() -> None:
    """Renew device tokens that are due, at start and twice a day, so a token
    cannot lapse just because nobody used the edge in its last 30 days."""
    while True:
        try:
            done = cloud.keep_fresh()
            if done:
                print(f"  device tokens: {done}", flush=True)
        except Exception as exc:              # never let the keeper die
            print(f"  device tokens: check failed: {exc}", flush=True)
        time.sleep(KEEP_FRESH_EVERY_S)


_INSTALLED = {"done": False, "background": False}


def install(app: Flask) -> None:
    """Everything the layer adds to a tool's app: the blueprint, the
    local-only guard and the job kinds. At import time in app.py; nothing
    runs in the background yet, so tests can import the app."""
    if _INSTALLED["done"]:
        return
    _INSTALLED["done"] = True
    _guard(app)
    app.register_blueprint(bp)
    JOBS.register("knowledge-sync", _sync_job)
    JOBS.register("first-start", firststart.run)


def start_background() -> None:
    """In main(), when the server really starts: the token keeper and first start."""
    if _INSTALLED["background"]:
        return
    _INSTALLED["background"] = True
    threading.Thread(target=_keep_tokens_fresh, name="token-keeper", daemon=True).start()
    start_first_start()
