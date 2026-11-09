# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 D-LAB-5
"""
chat.py — the edge's chat panel: Claude, with this edge's tools and nothing
else. Owned by the edge layer; the prompt and what may be proposed are in
settings.py.

The page talks to Claude Code running on this computer (`claude -p`), signed
in with the person's own claude.ai account: no API key, no extra bill.
Claude Code gets:

    tools        none of its own (--tools ""): no shell, no files, no web
    MCP          only this edge's MCP server (--strict-mcp-config), started in
                 panel mode: every change it asks for is a PROPOSAL that the
                 person confirms in the panel (<ENV>_CHAT_CONFIRM=1)
    not allowed  stop_server

What leaves the computer is what the tools answer. A proposal names one call
to this edge's own API, and only calls in settings.CHAT_ALLOWED can be
proposed. The person's click runs it.

What stays on this computer, in the edge's data folder (0600):
    chat-proposals.json   proposals still waiting for a click, so a restart
                          does not lose them; each is checked against
                          CHAT_ALLOWED again when read back
    chat-history.json     the last KEEP conversations, to read again and
                          continue (Claude remembers what was said)

The person's decisions reach Claude as a line before their next message
(Proposals.news), and the proposals() tool lists every proposal and what
became of it. A tool with a selection (items chosen on its page) passes it to
Chat.ask; a new one is announced to Claude once (settings.CHAT_SELECTION_NOTICE).
"""

from __future__ import annotations

import collections
import itertools
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import settings

REPO = Path(__file__).resolve().parents[2]
SERVER = settings.TOOL.replace("-", "_")          # the MCP server's name as Claude Code sees it


def allowed(method: str, path: str) -> bool:
    """Whether the chat may propose this call (settings.CHAT_ALLOWED)."""
    return any(str(method).upper() == m and re.fullmatch(p, str(path)) for m, p in settings.CHAT_ALLOWED)


# p<run>-<n>: the run part differs from one start of the edge to the next, so
# a card left on the page from before a restart never confirms a newer one
PROPOSAL_ID = r"p[0-9a-f]{6}-\d{1,9}"


class Proposals:
    """Changes Claude asked for, waiting for the person's click."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._run = secrets.token_hex(3)
        self.items: Dict[str, Dict[str, Any]] = {}
        self._news: List[Dict[str, Any]] = []      # decided since the chat last heard
        self.path = path
        for p in self._read():
            self.items[p["id"]] = p

    def _read(self) -> List[Dict[str, Any]]:
        if not self.path or not self.path.is_file():
            return []
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for p in saved if isinstance(saved, list) else []:
            try:
                if (p.get("state") == "waiting" and isinstance(p.get("body"), dict)
                        and re.fullmatch(PROPOSAL_ID, str(p.get("id"))) and allowed(p["method"], p["path"])):
                    out.append({k: p[k] for k in ("id", "summary", "method", "path", "body", "state", "at")})
            except (AttributeError, KeyError, TypeError):
                continue
        return out

    def _save(self) -> None:
        if not self.path:
            return
        waiting = [p for p in self.items.values() if p["state"] == "waiting"]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(waiting, ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass                                     # still in memory; the next change tries again

    def add(self, summary: str, method: str, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        method = str(method).upper()
        if not allowed(method, path):
            raise ValueError(f"{method} {path} cannot be proposed from the chat")
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        with self._lock:
            pid = f"p{self._run}-{next(self._ids)}"
            self.items[pid] = {"id": pid, "summary": str(summary)[:300], "method": method, "path": path,
                               "body": body, "state": "waiting", "at": time.time()}
            self._save()
            return dict(self.items[pid])

    def take(self, pid: str, state: str) -> Dict[str, Any]:
        """Mark a waiting proposal confirmed or dismissed (once)."""
        with self._lock:
            p = self.items.get(pid)
            if not p:
                raise KeyError(pid)
            if p["state"] != "waiting":
                raise ValueError(f"this proposal is {p['state']} already")
            p["state"] = state
            if state == "dismissed":
                self._news.append({"id": pid, "summary": p["summary"], "state": "dismissed"})
            self._save()
            return dict(p)

    def ran(self, pid: str, ok: bool, problem: Optional[str] = None) -> None:
        """What happened when a confirmed proposal ran: told to the chat with
        the next message, and kept for proposals()."""
        with self._lock:
            p = self.items.get(pid)
            if not p:
                return
            p["outcome"] = "done" if ok else "failed"
            if problem:
                p["problem"] = str(problem)[:300]
            self._news.append({"id": pid, "summary": p["summary"], "state": p["outcome"], "problem": p.get("problem")})

    def news(self) -> str:
        """The person's decisions since the chat last heard, as one line to put
        before their next message (empty if none), forgotten once told."""
        with self._lock:
            told, self._news = self._news, []
        if not told:
            return ""
        word = {"done": "confirmed, it ran", "failed": "confirmed, but it FAILED", "dismissed": "dismissed"}
        parts = [f"{n['id']} ({n['summary']}): {word[n['state']]}" + (f": {n['problem']}" if n.get("problem") else "")
                 for n in told]
        return ("[In the panel since your last answer: " + "; ".join(parts)
                + ". Only these are decided; call proposals() for the rest.]\n\n")

    def listing(self) -> List[Dict[str, Any]]:
        """Every proposal of this run and what became of it (the proposals() tool)."""
        with self._lock:
            return [{k: p.get(k) for k in ("id", "summary", "state", "outcome", "problem")} for p in self.items.values()]


def available() -> Dict[str, Any]:
    exe = shutil.which("claude")
    if not exe:
        return {"available": False, "why": "Claude Code (`claude`) is not installed on this computer"}
    try:
        v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        v = ""
    return {"available": True, "version": v}


def mcp_config(workdir: Path, port: int) -> Path:
    """This edge's MCP server, in panel mode, as Claude Code starts it."""
    cfg = {"mcpServers": {SERVER: {
        "command": sys.executable,
        "args": ["-m", f"{settings.PACKAGE}.mcp_server", "--no-autostart", "--port", str(port)],
        "env": {"PYTHONPATH": str(REPO), f"{settings.ENV}_CHAT_CONFIRM": "1"},
    }}}
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = workdir / "mcp.json"
    path.write_text(json.dumps(cfg))
    os.chmod(path, 0o600)
    return path


# How proposals work, added after the tool's own prompt (settings.CHAT_SYSTEM):
# the layer's behaviour, so an edge whose settings.py predates it gets it too.
RULES = """
How the panel works: the person's decisions on your proposals reach you as a
line in brackets before their next message ("In the panel since your last
answer: …"), and proposals() lists every proposal with its state and, once
run, whether it worked and why not. Check it before telling the person what
to confirm or dismiss: never ask them to decide one that is decided already,
and when one failed, say why and propose it again, fixed."""


def system_prompt(lang: str) -> str:
    return settings.CHAT_SYSTEM.format(lang=lang) + "\n" + RULES


def command(message: str, workdir: Path, port: int, lang: str, session: Optional[str]) -> List[str]:
    args = [shutil.which("claude") or "claude", "-p", message,
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--tools", "",
            "--strict-mcp-config", "--mcp-config", str(mcp_config(workdir, port)),
            "--allowedTools", f"mcp__{SERVER}",
            "--disallowedTools", f"mcp__{SERVER}__stop_server",
            "--append-system-prompt", system_prompt(lang)]
    if session:
        args += ["--resume", session]
    return args


def events(lines: Iterator[str]) -> Iterator[Dict[str, Any]]:
    """Claude Code's stream-json, as the panel's events:

        {"t": "text", "text": …}             a piece of the answer
        {"t": "tool", "name": …, "id": …}    a tool Claude calls
        {"t": "proposal", "id": …}           a change waiting for a click
        {"t": "done", "session": …, "error": …}
    """
    streamed = False
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        kind = e.get("type")
        if kind == "stream_event":
            ev = e.get("event") or {}
            delta = ev.get("delta") or {}
            if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                streamed = True
                yield {"t": "text", "text": delta.get("text", "")}
            elif ev.get("type") == "message_start":
                streamed = False
        elif kind == "assistant":
            for c in (e.get("message") or {}).get("content") or []:
                if c.get("type") == "text" and not streamed:
                    yield {"t": "text", "text": c.get("text", "")}
                elif c.get("type") == "tool_use":
                    yield {"t": "tool", "name": str(c.get("name", "")).replace(f"mcp__{SERVER}__", ""), "id": c.get("id")}
        elif kind == "user":
            for c in (e.get("message") or {}).get("content") or []:
                if not isinstance(c, dict) or c.get("type") != "tool_result":
                    continue
                text = c.get("content")
                if isinstance(text, list):
                    text = " ".join(x.get("text", "") for x in text if isinstance(x, dict))
                m = re.search(r'"proposal":\s*"(' + PROPOSAL_ID + ')"', str(text or ""))
                if m:
                    yield {"t": "proposal", "id": m.group(1)}
        elif kind == "result":
            yield {"t": "done", "session": e.get("session_id"),
                   "error": e.get("result") if e.get("is_error") else None}


class Chat:
    """One conversation at a time; a new message continues it (--resume)."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.session: Optional[str] = None
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self.told: Optional[str] = None             # the selection this conversation was last told of

    def new(self) -> None:
        self.stop()
        self.session = None
        self.told = None

    def with_selection(self, message: str, selection: Optional[str]) -> str:
        """The message as Claude gets it: when the person chose another
        selection since the last message, a line first saying so. Without it
        a continued conversation answers from the items it remembers."""
        if not selection or selection == self.told:
            return message
        self.told = selection
        return selection_notice() + "\n\n" + message

    def stop(self) -> None:
        p = self.proc
        if p and p.poll() is None:
            p.kill()

    def ask(self, message: str, port: int, lang: str, selection: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        """selection: anything that names what the person has chosen on the
        page (a file, a hash); only whether it changed matters here."""
        if not self._lock.acquire(blocking=False):
            yield {"t": "done", "error": "Claude is still answering the last message."}
            return
        try:
            message = self.with_selection(message, selection)
            self.workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "ANTHROPIC_API_KEY")}
            self.proc = subprocess.Popen(command(message, self.workdir, port, lang, self.session),
                                         cwd=self.workdir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, env=env)
            assert self.proc.stdout is not None and self.proc.stderr is not None
            # drain stderr all the time: a full pipe would block Claude Code
            # and the panel would wait for ever (as ffmpeg once did)
            tail: "collections.deque[str]" = collections.deque(maxlen=20)
            err = self.proc.stderr
            threading.Thread(target=lambda: [tail.append(x.rstrip()) for x in err], daemon=True).start()
            finished = False
            for ev in events(self.proc.stdout):
                if ev["t"] == "done":
                    finished = True
                    if ev.get("session"):
                        self.session = ev["session"]
                yield ev
            self.proc.wait()
            if not finished:
                yield {"t": "done", "error": ("\n".join(tail)[-300:] or "stopped")}
        finally:
            self.proc = None
            self._lock.release()


# ------------------------------------------------------- settings hooks --
#
# settings.py belongs to the tool and is written once, so an edge made before
# these existed has none of them: each has a default here.

DEFAULT_SELECTION_NOTICE = ("[The person chose a new selection on the page. It replaces any earlier one: "
                            "read it again before answering, and work only on those items.]")


def selection_notice() -> str:
    return getattr(settings, "CHAT_SELECTION_NOTICE", None) or DEFAULT_SELECTION_NOTICE


def title_skip() -> List[str]:
    """Openings of messages the page writes itself (a selection's prompt, say),
    which make a poor title for a conversation."""
    return list(getattr(settings, "CHAT_TITLE_SKIP", None) or [])


# --------------------------------------------------------------- history --
#
# Claude Code keeps every conversation as <session>.jsonl in a folder named
# after the working folder. The chat gets a new, unguessable working folder
# each run (routes.chat_state), so after a restart Claude would not find the
# conversation to --resume. The history remembers the last KEEP
# conversations and where their file is; continuing one copies its file into
# this run's folder first. Everything stays on this computer.

KEEP = 10
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
NEWS = re.compile(r"^\[In the panel since your last answer:[^\]]*\]\s*")


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def transcript_path(workdir: Path, session: str) -> Path:
    """Where Claude Code keeps a conversation held in that working folder."""
    return claude_home() / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workdir)) / f"{session}.jsonl"


def _texts(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _own_words(text: str) -> str:
    """A message as the person wrote it: the lines the edge put first taken off."""
    for _ in range(2):
        text = NEWS.sub("", text)
        if text.startswith(selection_notice()):
            text = text[len(selection_notice()):].lstrip()
    return text


def transcript(path: Path) -> List[Dict[str, Any]]:
    """What was said, in order: [{who: me|claude, text, at}]. Tool calls and
    their results are left out, and so are the lines the edge added."""
    out: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") not in ("user", "assistant") or not isinstance(e.get("message"), dict):
            continue
        text = _texts(e["message"].get("content")).strip()
        if e["type"] == "user":
            text = _own_words(text)
        if text:
            who = "me" if e["type"] == "user" else "claude"
            if out and out[-1]["who"] == who == "claude":
                out[-1]["text"] += "\n\n" + text           # one answer in several pieces
            else:
                out.append({"who": who, "text": text, "at": e.get("timestamp")})
    return out


def _title(said: List[Dict[str, Any]]) -> str:
    """The first thing the person wrote in their own words."""
    mine = [m["text"] for m in said if m["who"] == "me"]
    skip = tuple(title_skip())
    own = next((m for m in mine if not (skip and m.startswith(skip))), mine[0] if mine else "")
    one = " ".join(own.split())
    return one[:80] + ("…" if len(one) > 80 else "")


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class History:
    """The last KEEP conversations: [{session, file, title, started, updated,
    said}], newest first, in the edge's data folder (0600)."""

    def __init__(self, path: Path, keep: int = KEEP) -> None:
        self.path, self.keep = path, keep
        self._lock = threading.Lock()

    def _read(self) -> List[Dict[str, Any]]:
        try:
            items = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [i for i in items if isinstance(i, dict) and SESSION_ID.fullmatch(str(i.get("session", "")))
                and isinstance(i.get("file"), str)] if isinstance(items, list) else []

    def _write(self, items: List[Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def record(self, session: str, file: Path) -> Dict[str, Any]:
        """A conversation that just answered: first in the list."""
        if not SESSION_ID.fullmatch(session or ""):
            raise ValueError("not a conversation id")
        said = transcript(file)
        with self._lock:
            items = self._read()
            old = next((i for i in items if i["session"] == session), {})
            entry = {"session": session, "file": str(file), "title": old.get("title") or _title(said),
                     "started": old.get("started") or (said[0]["at"] if said else None) or _iso(),
                     "updated": (said[-1]["at"] if said else None) or _iso(), "said": len(said)}
            items = [entry] + [i for i in items if i["session"] != session]
            self._write(items[:self.keep])
            return entry

    def items(self) -> List[Dict[str, Any]]:
        """Newest first; a conversation whose file is gone is left out. Never
        the file's path: the page has no use for it."""
        return [{k: v for k, v in i.items() if k != "file"} for i in self._read() if Path(i["file"]).is_file()]

    def _hit(self, session: str) -> Dict[str, Any]:
        hit = next((i for i in self._read() if i["session"] == session and Path(i["file"]).is_file()), None)
        if not hit:
            raise KeyError(session)
        return hit

    def get(self, session: str) -> Dict[str, Any]:
        hit = self._hit(session)
        return {**{k: v for k, v in hit.items() if k != "file"}, "messages": transcript(Path(hit["file"]))}

    def resume(self, chat: "Chat", session: str) -> Dict[str, Any]:
        """Continue that conversation: its file copied into this run's working
        folder (where Claude Code looks for it), and the chat set to it."""
        hit = self._hit(session)
        here = transcript_path(chat.workdir, session)
        if Path(hit["file"]).resolve() != here.resolve():
            here.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(hit["file"], here)
            os.chmod(here, 0o600)
        chat.stop()
        chat.session, chat.told = session, None
        self.record(session, here)
        return self.get(session)
