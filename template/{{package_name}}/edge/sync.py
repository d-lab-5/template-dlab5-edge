# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 D-LAB-5
"""
sync.py — A-Boxes synchronised between this edge and its clouds (ADR-0007 of
template-dlab5-cloud). Owned by the edge layer.

An RDF graph is a set of statements, so two copies merge statement by
statement against the version both last agreed on (the BASE, kept per
connection in abox/.sync/<connection>/<graph>.ttl):

    merged = base − removed here − removed there + added here + added there

The one real disagreement is two different values for a fact that may have
only one (settings.SINGLE_VALUED: a name, a date …). That is a CONFLICT:
nothing is written, and the person picks "mine" or "theirs".

Nothing is written anywhere until the merged graph passes the shapes. The
cloud validates again, and refuses a write based on a version it no longer
holds (409); then the edge merges once more.

A local graph is found in the cloud by NAME the first time (the slug of the
space's name: ids differ between clouds) and by the space's ID from then on,
kept per connection in .sync/<connection>/spaces.json, so a space renamed in
the cloud stays the same graph here. A space that is gone from the cloud is
reported ("gone"), never deleted here.

settings.SYNC_SCOPE says where stage and prod meet. "merged": one local copy,
synced with every linked connection in turn. "per-connection": each
connection its own folder (knowledge.scope_dir), synced with it alone.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from rdflib import Graph, URIRef
from rdflib.compare import to_canonical_graph

from . import cloud, settings
from . import knowledge as kn

Triple = Tuple[Any, Any, Any]


def _single() -> Set[URIRef]:
    return {URIRef(p) for p in settings.SINGLE_VALUED}


def _per_language() -> Set[URIRef]:
    return {URIRef(p) for p in settings.PER_LANGUAGE}


def _set(g: Graph) -> Set[Triple]:
    """Statements with blank nodes named canonically, so two parses compare."""
    return set(to_canonical_graph(g))


def _graph(triples: Set[Triple]) -> Graph:
    g = Graph()
    for t in triples:
        g.add(t)
    return g


def _parse(ttl: str) -> Graph:
    g = Graph()
    if ttl.strip():
        g.parse(data=ttl, format="turtle")
    return g


class Base:
    """What this edge and one connection last agreed on, for one graph."""

    def __init__(self, kb: kn.KnowledgeBase, profile: str, graph: str):
        self.dir = kb.dir / ".sync" / profile
        self.ttl = self.dir / f"{graph}.ttl"
        self.meta = self.dir / f"{graph}.json"

    def load(self) -> Tuple[Graph, int]:
        if not self.ttl.exists():
            return Graph(), 0
        return _parse(self.ttl.read_text()), int(json.loads(self.meta.read_text())["version"])

    def save(self, g: Graph, version: int) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path, data in ((self.ttl, g.serialize(format="turtle")), (self.meta, json.dumps({"version": version}))):
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, path)


def merge(base: Set[Triple], mine: Set[Triple], theirs: Set[Triple],
          resolve: Optional[Dict[str, str]] = None) -> Tuple[Set[Triple], list]:
    """Three-way merge of statement sets; returns (merged, unresolved conflicts)."""
    single, per_lang = _single(), _per_language()
    added_mine, removed_mine = mine - base, base - mine
    added_theirs, removed_theirs = theirs - base, base - theirs
    merged = (base - removed_mine - removed_theirs) | added_mine | added_theirs
    conflicts = []
    resolve = resolve or {}

    def lang_of(p, o):
        return getattr(o, "language", None) if p in per_lang else None

    groups = {(t[0], t[1], lang_of(t[1], t[2])) for t in added_mine | added_theirs if t[1] in single or t[1] in per_lang}
    for s, p, lg in groups:
        def same(t, s=s, p=p, lg=lg):
            return t[0] == s and t[1] == p and lang_of(t[1], t[2]) == lg
        values = {t[2] for t in merged if same(t)}
        if len(values) < 2:
            continue
        key = f"{s} {p}" + (f"@{lg}" if lg else "")
        choice = resolve.get(key)
        if choice in ("mine", "theirs"):
            keep = mine if choice == "mine" else theirs
            merged = {t for t in merged if not same(t)} | {t for t in keep if same(t)}
            continue
        conflicts.append({"key": key, "subject": str(s), "property": str(p) + (f"@{lg}" if lg else ""),
                          "mine": sorted(str(t[2]) for t in mine if same(t)),
                          "theirs": sorted(str(t[2]) for t in theirs if same(t))})
    return merged, sorted(conflicts, key=lambda c: c["key"])


class SpaceMap:
    """Which space of a connection each local graph is: {graph: space id}."""

    def __init__(self, kb: kn.KnowledgeBase, profile: str):
        self.path = kb.dir / ".sync" / profile / "spaces.json"

    def load(self) -> Dict[str, str]:
        try:
            return dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def save(self, m: Dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(m, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)


def space_of(kb: kn.KnowledgeBase, graph: str, profile: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], bool]:
    """(the connection's space for a local graph, whether it was known by id
    and is now gone from the cloud)."""
    name = profile or cloud.active_profile()
    known = SpaceMap(kb, name).load().get(graph)
    spaces = cloud.tenant(name).get("spaces") or []
    if known:
        hit = next((s for s in spaces if s.get("id") == known), None)
        return hit, hit is None
    taken = set(SpaceMap(kb, name).load().values())
    hit = cloud.space_by_slug(graph, name)
    return (hit if hit and hit.get("id") not in taken else None), False


def sync(kb: kn.KnowledgeBase, graph: str, profile: Optional[str] = None,
         resolve: Optional[Dict[str, str]] = None, _attempt: int = 0) -> Dict[str, Any]:
    """Synchronise one local graph with one connection."""
    name = profile or cloud.active_profile()
    # The base is kept per remote graph, so linking into another tenant never
    # merges against a stale base.
    space, gone = space_of(kb, graph, name)
    if space is None:
        return {"graph": graph, "profile": name, "status": "gone" if gone else "no_space",
                "error": f"{name} has no {settings.SPACE_NOUN} for the A-Box “{graph}”"
                         + (" any more: it was removed there; nothing here is deleted" if gone else "")}
    m = SpaceMap(kb, name)
    known = m.load()
    if known.get(graph) != space["id"]:
        known[graph] = space["id"]
        m.save(known)
    remote_name = space["id"]
    base_store = Base(kb, name, remote_name)
    base_graph, _ = base_store.load()
    remote = cloud.call("graphs/get", {"name": remote_name}, name)
    theirs_graph, remote_version = _parse(remote.get("ttl") or ""), int(remote.get("version") or 0)
    mine_graph = kb.load(graph)

    base, mine, theirs = _set(base_graph), _set(mine_graph), _set(theirs_graph)
    merged, conflicts = merge(base, mine, theirs, resolve)
    stats = {"graph": graph, "profile": name, "pulled": len(theirs - base), "removed_there": len(base - theirs),
             "pushed": len(mine - base), "removed_here": len(base - mine)}
    if conflicts:
        return {**stats, "status": "conflict", "conflicts": conflicts}

    merged_graph = _graph(merged)
    ok, problems = kn.validate(merged_graph)
    if not ok:
        return {**stats, "status": "invalid", "problems": problems}

    version = remote_version
    if merged != theirs:
        try:
            out = cloud.call("graphs/put", {"name": remote_name, "ttl": merged_graph.serialize(format="turtle"),
                                            "baseVersion": remote_version}, name)
            version = int(out["version"])
        except cloud.CloudError as exc:
            if exc.status == 409 and _attempt < 2:          # someone saved meanwhile: merge again
                return sync(kb, graph, name, resolve, _attempt + 1)
            if exc.status == 422:
                return {**stats, "status": "invalid", "problems": exc.payload.get("problems", [str(exc)])}
            raise
    if merged != mine:
        def replace(g: Graph) -> None:
            g.remove((None, None, None))
            for t in merged:
                g.add(t)
        kb.change(graph, replace)
    base_store.save(merged_graph, version)
    return {**stats, "status": "ok", "version": version, "statements": len(merged)}


def sync_everywhere(kb: kn.KnowledgeBase, graph: str,
                    resolve: Optional[Dict[str, Dict[str, str]]] = None) -> Dict[str, Any]:
    """Every linked connection in turn. The local graph collects what each
    brings, so a statement added in stage reaches prod on the next round."""
    out = {}
    names = cloud.linked_profiles()
    if cloud.per_connection():               # stage and prod never meet here
        names = [n for n in names if n == cloud.active_profile()]
    for name in names:
        try:
            out[name] = sync(kb, graph, name, (resolve or {}).get(name))
        except cloud.CloudError as exc:
            out[name] = {"status": "error", "error": str(exc)}
    return out


def sync_all(kb: kn.KnowledgeBase, profile: Optional[str] = None) -> Dict[str, Any]:
    """Every space of a connection's tenant: its A-Box pulled (created locally
    when new) and merged, under the graph it already has here, else its slug.
    A local graph whose space is gone from the cloud is reported, not deleted.
    What first start runs after linking, and the refresh of the space list."""
    name = profile or cloud.active_profile()
    out: Dict[str, Any] = {}
    spaces = cloud.tenant(name, refresh=True).get("spaces") or []
    by_id = {v: g for g, v in SpaceMap(kb, name).load().items()}
    for space in spaces:
        graph = by_id.get(space.get("id")) or cloud.space_slug(space.get("name"))
        out[graph] = sync(kb, graph, name).get("status")
    ids = {s.get("id") for s in spaces}
    for graph, sid in SpaceMap(kb, name).load().items():
        if sid not in ids:
            out[graph] = "gone"
    return out


def listing(kb: kn.KnowledgeBase, profile: Optional[str] = None) -> List[Dict[str, Any]]:
    """A connection's spaces as this edge knows them (no call when the tenant
    is cached): each with the local graph it is or will be, and the graphs
    whose space is gone from the cloud, marked so."""
    name = profile or cloud.active_profile()
    m = SpaceMap(kb, name)
    known = m.load()
    by_id = {v: g for g, v in known.items()}
    spaces = cloud.tenant(name).get("spaces") or []
    # a space seen for the first time keeps the graph it is given now, so a
    # rename before its first sync does not make it a second graph
    fresh = {}
    for s in spaces:
        sid = s.get("id")
        if sid and sid not in by_id:
            g = cloud.space_slug(s.get("name"))
            if g not in known and g not in fresh:
                fresh[g] = sid
                by_id[sid] = g
    if fresh:
        m.save({**known, **fresh})
        known = {**known, **fresh}
    out = [{"graph": by_id.get(s.get("id")) or cloud.space_slug(s.get("name")), "id": s.get("id"),
            "name": s.get("name"), "kind": s.get("kind"), "gone": False} for s in spaces]
    ids = {s.get("id") for s in spaces}
    out += [{"graph": g, "id": sid, "name": None, "kind": None, "gone": True} for g, sid in known.items() if sid not in ids]
    return out
