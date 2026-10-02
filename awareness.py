"""CHANGE AWARENESS — what changed in Dry that the agent did not do itself.

Two consumers, one description:
  · the agent, each turn ("since we last talked" at the start of a session, "just changed" during it) — by POLLING the
    change feed of the watched spaces from a per-profile cursor (one small request per space; nothing runs between turns);
  · the person, live — by SSE inside the Hermes GATEWAY (the one long-running Hermes process): one stream per watched
    space; Dry pushes each change the instant it is written, replays everything after Last-Event-ID on reconnect (so a
    restart misses nothing), and the batch is sent to the person's chosen target through Hermes's send_message tool —
    plain text, no model call.
Dry's change feed says WHO changed something, not through which door; the agent's own writes (this plugin's and Dry's MCP
tools') are recorded as they happen and filtered out, so "you changed" means the person did it (web app, phone, another tool).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .dry_client import DryClient, DryError, field_value

logger = logging.getLogger(__name__)
CURSORS = "dry-cursors.json"
OWN = "dry-own-writes.json"
RECENT = "dry-recent-changes.json"
OWN_TTL_S = 6 * 3600
RECENT_KEEP = 300
MAX_WATCHED = 20
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}")
_file_lock = threading.Lock()


# ---- small per-profile files ------------------------------------------------------------------------------------
def _read(home: str, name: str, default: Any) -> Any:
    try:
        p = Path(home) / name
        return json.loads(p.read_text()) if p.exists() else default
    except Exception:
        return default


def _write(home: str, name: str, data: Any) -> None:
    p = Path(home) / name
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(p)


def mark_own(home: str, ids: Iterable[str]) -> None:
    """Record ids the AGENT just wrote, so the change feed never reports them back as the person's."""
    ids = [i for i in ids if i]
    if not ids:
        return
    with _file_lock:
        own = _read(home, OWN, {})
        now = time.time()
        own = {k: v for k, v in own.items() if now - v < OWN_TTL_S}
        for i in ids:
            own[i] = now
        if len(own) > 2000:
            own = dict(sorted(own.items(), key=lambda kv: kv[1])[-2000:])
        _write(home, OWN, own)


def own_ids(home: str) -> set:
    now = time.time()
    return {k for k, v in _read(home, OWN, {}).items() if now - v < OWN_TTL_S}


def ids_in(*blobs: Any) -> List[str]:
    out: List[str] = []
    for b in blobs:
        try:
            text = b if isinstance(b, str) else json.dumps(b, default=str)
        except Exception:
            continue
        out += UUID_RE.findall(text or "")
    return out


def remember_recent(home: str, items: List[dict]) -> None:
    if not items:
        return
    with _file_lock:
        rec = _read(home, RECENT, [])
        seen = {(r.get("spaceId"), r.get("seq")) for r in rec}
        rec += [i for i in items if (i.get("spaceId"), i.get("seq")) not in seen]
        _write(home, RECENT, rec[-RECENT_KEEP:])


def recent(home: str, hours: float = 24.0, space: str = "") -> List[dict]:
    cutoff = time.time() - hours * 3600
    out = []
    for r in _read(home, RECENT, []):
        try:
            t = datetime.fromisoformat(r["at"].replace("Z", "+00:00")).timestamp()
        except Exception:
            t = 0
        if t >= cutoff and (not space or space.lower() in (r.get("space") or "").lower() or space == r.get("spaceId")):
            out.append(r)
    return out


# ---- which spaces ------------------------------------------------------------------------------------------------
MEMBER_ROLES = {"space_owner", "admin", "builder", "member"}


def watched_spaces(client: DryClient, setting: Any = "all") -> List[dict]:
    """The spaces the person belongs to (never the ones they only see as a site admin), or the named ones."""
    spaces = client.spaces()
    if isinstance(setting, list) and setting:
        want = {str(x).lower() for x in setting}
        return [s for s in spaces if s.get("id") in want or (s.get("name") or "").lower() in want][:MAX_WATCHED]
    return [s for s in spaces if MEMBER_ROLES & set(s.get("roles") or [s.get("role")])][:MAX_WATCHED]


# ---- turning change events into sentences ------------------------------------------------------------------------
class Describer:
    """Caches type and member names per space for the life of one digest."""

    def __init__(self, client: DryClient, me: str):
        self.c, self.me = client, me
        self.types: Dict[str, Dict[str, dict]] = {}
        self.people: Dict[str, Dict[str, str]] = {}

    def _types(self, sid: str) -> Dict[str, dict]:
        if sid not in self.types:
            try:
                self.types[sid] = {t["id"]: t for t in self.c.types(sid)}
            except DryError:
                self.types[sid] = {}
        return self.types[sid]

    def who(self, sid: str, uid: str) -> str:
        if uid == self.me:
            return "you"
        if sid not in self.people:
            try:
                self.people[sid] = {m.get("userId") or m.get("id"): (m.get("displayName") or m.get("name") or m.get("email") or "someone") for m in self.c.members(sid)}
            except DryError:
                self.people[sid] = {}
        return self.people[sid].get(uid, "someone")

    def title(self, sid: str, obj: dict) -> tuple[str, str]:
        t = self._types(sid).get(obj.get("typeId")) or {}
        name = field_value(obj, t, "Title") or field_value(obj, t, "Name") or obj.get("title")
        if not name:
            for f in t.get("fields", []):
                v = (obj.get("values") or {}).get(f.get("id"))
                if f.get("kind") in ("text", "textarea") and isinstance(v, str) and v.strip():
                    name = v
                    break
        name = " ".join(str(name or "").split())
        return (name[:80] + "…" if len(name) > 80 else name) or "(untitled)", t.get("name") or ""

    def describe(self, space: dict, events: List[dict]) -> List[dict]:
        sid, sname = space["id"], space.get("name") or "a space"
        last: Dict[str, dict] = {}
        first_op: Dict[str, str] = {}
        for e in events:                                   # one line per thing: created+edited = created
            k = f"{e.get('kind')}:{e.get('id')}"
            first_op.setdefault(k, e.get("op"))
            last[k] = e
        out = []
        for k, e in last.items():
            op = "create" if first_op[k] == "create" and e.get("op") != "delete" else e.get("op")
            item = {"seq": e.get("seq"), "spaceId": sid, "space": sname, "at": e.get("at"), "op": op, "kind": e.get("kind"),
                    "actor": self.who(sid, e.get("actorId")), "actorIsMe": e.get("actorId") == self.me, "id": e.get("id"), "title": "", "type": "", "url": ""}
            if e.get("kind") == "object" and op != "delete":
                try:
                    obj = self.c.get_object(sid, e["id"])
                except DryError:
                    continue                                 # gone since, or not visible to this person
                item["title"], item["type"] = self.title(sid, obj)
                item["url"] = self.c.record_url(e["id"], obj.get("typeId"))
                if item["type"] == "Comment" and obj.get("parentId"):
                    try:
                        parent = self.c.get_object(sid, obj["parentId"])
                        item["parent"] = self.title(sid, parent)[0]
                        item["url"] = self.c.record_url(obj["parentId"], parent.get("typeId"))
                    except DryError:
                        pass
            elif e.get("kind") == "type":
                t = self._types(sid).get(e["id"]) if op != "delete" else None
                item["title"], item["type"] = (t or {}).get("name", "a type"), "type"
            out.append(item)
        return out


VERB = {"create": "added", "update": "edited", "delete": "deleted"}


def sentence(i: dict) -> str:
    who = i.get("actor") or "someone"
    if i.get("kind") == "object":
        if i.get("type") == "Comment":
            return f"{who} commented on “{i.get('parent') or 'a record'}”: {i.get('title')}" + (f" · {i['url']}" if i.get("url") else "")
        if i.get("op") == "delete":
            return f"{who} deleted a record"
        return f"{who} {VERB.get(i.get('op'), 'changed')} {i.get('type') or 'record'} “{i.get('title')}”" + (f" · {i['url']}" if i.get("url") else "")
    if i.get("kind") == "type":
        return f"{who} {VERB.get(i.get('op'), 'changed')} the type “{i.get('title')}”"
    if i.get("kind") == "membership":
        return f"{who} changed who is in the space"
    if i.get("kind") == "space":
        return f"{who} changed the space's settings"
    return f"{who} changed something"


def digest(items: List[dict], header: str, cap: int = 15) -> str:
    if not items:
        return ""
    by: Dict[str, List[dict]] = {}
    for i in items:
        by.setdefault(i["space"], []).append(i)
    lines, n = [header], 0
    for space, its in by.items():
        lines.append(f"**{space}**")
        for i in its:
            if n >= cap:
                break
            lines.append(f"- {sentence(i)}")
            n += 1
    if len(items) > n:
        lines.append(f"- …and {len(items) - n} more")
    return "\n".join(lines)


# ---- polling (the agent's view) ----------------------------------------------------------------------------------
def collect(client: DryClient, home: str, key: str, spaces: List[dict], me: str, *, skip_own_edits: bool = False,
            live: Optional[Callable[[str, int], Optional[List[dict]]]] = None) -> List[dict]:
    """Changes after this profile's `key` cursor in each space, minus the agent's own writes; advances the cursor.
    A space seen for the first time sets its cursor at the end of the feed (no flood of old history)."""
    with _file_lock:
        cursors = _read(home, CURSORS, {})
    cur = dict(cursors.get(key, {}))
    own = own_ids(home)
    d = Describer(client, me)

    def one(space):
        sid = space["id"]
        try:
            if sid not in cur:
                return sid, client.latest_seq(sid), []
            streamed = live(sid, cur[sid]) if live else None      # the gateway's SSE stream already holds them: no request
            if streamed is not None:
                keep = [e for e in streamed if e.get("id") not in own and not (skip_own_edits and e.get("actorId") == me)]
                return sid, max([cur[sid]] + [e.get("seq", 0) for e in streamed]), d.describe(space, keep) if keep else []
            evs, since = [], cur[sid]
            for _ in range(3):
                r = client.changes(sid, since)
                evs += r.get("changes", [])
                since = r.get("nextSince", since)
                if not r.get("hasMore"):
                    break
            keep = [e for e in evs if e.get("id") not in own and not (skip_own_edits and e.get("actorId") == me)]
            return sid, since, d.describe(space, keep) if keep else []
        except DryError as e:
            logger.debug("dry: changes for %s failed: %s", sid, e)
            return sid, cur.get(sid), []

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(one, spaces))
    items: List[dict] = []
    for sid, seq, its in results:
        if seq is not None:
            cur[sid] = seq
        items += its
    with _file_lock:
        cursors = _read(home, CURSORS, {})
        cursors[key] = {**cursors.get(key, {}), **cur}
        _write(home, CURSORS, cursors)
    items.sort(key=lambda i: i.get("at") or "")
    remember_recent(home, items)
    return items


# ---- SSE (live alerts, gateway only) -----------------------------------------------------------------------------
class Watcher:
    """One SSE stream per watched space; batches what arrives and sends it to the person's target."""

    def __init__(self, client: DryClient, home: str, me: str, settings: Callable[[], dict], send: Callable[[str, str], Any],
                 spawn: Callable[..., threading.Thread], flush_s: float = 15.0):
        self.c, self.home, self.me, self.settings, self.send, self.spawn = client, home, me, settings, send, spawn
        self.flush_s = flush_s
        self.stop = threading.Event()
        self.buf: Dict[str, List[dict]] = {}
        self.spaces: Dict[str, dict] = {}
        self.threads: Dict[str, threading.Thread] = {}
        self.lock = threading.Lock()
        self.sent: List[str] = []          # for tests / status
        self.seen: Dict[str, List[dict]] = {}      # every event each stream delivered (the agent's digest reads these)
        self.from_seq: Dict[str, int] = {}         # the stream covers everything after this seq
        self.live: Dict[str, bool] = {}
        self.reacted: List[float] = []             # times of agent reactions (rate cap)
        self.react: Optional[Callable[[List[dict]], bool]] = None   # starts an agent turn; True when the host accepted it

    def start(self) -> None:
        st = self.settings()
        logger.info("dry: watching your spaces live (pid %s) · alerts %s · reactions %s", os.getpid(),
                    ("→ " + st["notify_to"]) if st.get("notify_to") else "off", "on" if st.get("react") else "off")
        self.spawn(self._supervise, name="dry-watch-supervisor").start()
        self.spawn(self._flusher, name="dry-watch-flusher").start()

    def _supervise(self) -> None:
        while not self.stop.is_set():
            try:
                for s in watched_spaces(self.c, self.settings().get("watch") or "all"):
                    self.spaces[s["id"]] = s
                    t = self.threads.get(s["id"])
                    if t is None or not t.is_alive():
                        self.threads[s["id"]] = self.spawn(lambda sp=s: self._stream(sp), name=f"dry-sse-{s['id'][-6:]}")
                        self.threads[s["id"]].start()
            except DryError as e:
                logger.warning("dry: watcher could not list spaces: %s", e)
            self.stop.wait(120)          # new spaces are picked up within two minutes

    def _cursor(self, sid: str) -> Optional[int]:
        return _read(self.home, CURSORS, {}).get("watch", {}).get(sid)

    def _set_cursor(self, sid: str, seq: int) -> None:
        with _file_lock:
            c = _read(self.home, CURSORS, {})
            w = c.setdefault("watch", {})
            if seq > w.get(sid, -1):
                w[sid] = seq
                _write(self.home, CURSORS, c)

    def events_since(self, sid: str, seq: int) -> Optional[List[dict]]:
        """What this space's live stream delivered after `seq` — or None when the stream is down or does not reach back that far
        (then the caller polls)."""
        with self.lock:
            if not self.live.get(sid) or seq < self.from_seq.get(sid, 1 << 62):
                return None
            return [e for e in self.seen.get(sid, []) if e.get("seq", 0) > seq]

    def _stream(self, space: dict) -> None:
        sid, backoff = space["id"], 2.0
        if self._cursor(sid) is None:
            try:
                self._set_cursor(sid, self.c.latest_seq(sid))
            except DryError:
                pass
        while not self.stop.is_set():
            try:
                start = self._cursor(sid)
                with self.lock:
                    if sid not in self.from_seq and start is not None:
                        self.from_seq[sid] = start
                    self.live[sid] = True
                for ev in self.c.stream_events(sid, start):
                    backoff = 2.0
                    with self.lock:
                        self.buf.setdefault(sid, []).append(ev)
                        seen = self.seen.setdefault(sid, [])
                        seen.append(ev)
                        if len(seen) > 2000:
                            del seen[:1000]
                            self.from_seq[sid] = seen[0].get("seq", 0) - 1
                    if self.stop.is_set():
                        return
            except DryError as e:
                with self.lock:
                    self.live[sid] = False
                if e.status in (401, 403, 404):
                    logger.warning("dry: stopped watching %s: %s", space.get("name"), e)
                    return
                logger.debug("dry: stream %s dropped (%s); reconnecting", space.get("name"), e)
            self.stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)

    def _flusher(self) -> None:
        while not self.stop.wait(self.flush_s):
            try:
                self.flush()
            except Exception as e:
                logger.warning("dry: sending changes failed: %s", e)

    def flush(self) -> Optional[str]:
        with self.lock:
            buf, self.buf = self.buf, {}
        if not buf:
            return None
        st = self.settings()
        own = own_ids(self.home)
        d = Describer(self.c, self.me)
        items: List[dict] = []
        for sid, evs in buf.items():
            keep = [e for e in evs if e.get("id") not in own and (st.get("notify_own") or e.get("actorId") != self.me)]
            if keep:
                items += d.describe(self.spaces.get(sid) or {"id": sid, "name": "a space"}, keep)
            self._set_cursor(sid, max(e.get("seq", 0) for e in evs))
        remember_recent(self.home, items)
        if not items:
            return None
        # 1. let the agent react (default on): other people's changes, batched, at most react_per_hour
        others = [i for i in items if not i.get("actorIsMe")]
        if others and st.get("react") and self.react is not None:
            now = time.time()
            self.reacted = [t for t in self.reacted if now - t < 3600]
            if len(self.reacted) < int(st.get("react_per_hour") or 6):
                try:
                    if self.react(others):
                        self.reacted.append(now)
                        self._agent_told(items)
                        logger.info("dry: the agent is reacting to %d change(s)", len(others))
                        return "react"
                except Exception as e:
                    logger.warning("dry: reaction not started: %s", e)
            else:
                logger.info("dry: reaction cap reached (%s/hour) — plain alert instead", st.get("react_per_hour") or 6)
        # 2. otherwise a plain alert, when a target is set
        target = st.get("notify_to")
        if not target:
            return None
        text = digest(items, "🔔 New in Dry")
        logger.info("dry: sending %d change(s) to %s", len(items), target)
        self.send(target, text)
        self.sent.append(text)
        return text

    def _agent_told(self, items: List[dict]) -> None:
        """The agent has just been shown these; advance its own cursor so its next turn does not repeat them — only in spaces
        where the batch was ALL other people's (an edit of the person's own, not part of the reaction, must still reach the
        agent's next digest)."""
        by: Dict[str, List[dict]] = {}
        for i in items:
            by.setdefault(i["spaceId"], []).append(i)
        with _file_lock:
            c = _read(self.home, CURSORS, {})
            a = c.setdefault("agent", {})
            for sid, its in by.items():
                if any(i.get("actorIsMe") for i in its):
                    continue
                top = max(i.get("seq") or 0 for i in its)
                if top > a.get(sid, -1):
                    a[sid] = top
            _write(self.home, CURSORS, c)


# ---- one watcher per Hermes home, only in the gateway process ----------------------------------------------------
import sys as _sys
import types as _types
_SHARED = _sys.modules.setdefault("_dry_hermes_shared", _types.ModuleType("_dry_hermes_shared"))
if not hasattr(_SHARED, "watchers"):
    _SHARED.ctx, _SHARED.watchers = None, {}
_watchers: Dict[str, Watcher] = _SHARED.watchers     # one per Hermes home across both module copies
_lockfiles: Dict[str, Any] = {}


def in_gateway(home: str, wait_s: float = 0.0) -> bool:
    """True when this process is the Hermes gateway for `home` (it writes gateway.pid with its own pid)."""
    deadline = time.time() + wait_s
    while True:
        try:
            info = json.loads((Path(home) / "gateway.pid").read_text())
            if int(info.get("pid", -1)) == os.getpid():
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(2.0)


def acquire(home: str) -> bool:
    """An OS lock so exactly one process per Hermes home runs the watcher."""
    try:
        import fcntl
        f = open(Path(home) / "dry-watch.lock", "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lockfiles[home] = f
        return True
    except (OSError, ImportError):
        return False


def watcher_for(home: str) -> Optional[Watcher]:
    return _watchers.get(home)


def start_watcher(home: str, make: Callable[[], Watcher]) -> Optional[Watcher]:
    if home in _watchers:
        return _watchers[home]
    if not acquire(home):
        return None
    w = make()
    _watchers[home] = w
    w.start()
    return w
