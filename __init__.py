"""Dry for Hermes Agent — Dry as Hermes's long-term memory, plus the know-how to use Dry well.

One plugin, two kinds (plugin.yaml `kind: standalone`):
  · MEMORY PROVIDER `dry` (memory.provider: dry) — the person's Dry "Memories" space is the agent's long-term memory:
      - before each turn, the memories relevant to the message are recalled (hybrid search) and added to it;
      - whatever Hermes saves to its own memory (MEMORY.md / USER.md) is mirrored as a Memory record — add, replace, remove;
      - tools: dry_remember · dry_recall (Memories, or every space) · dry_forget.
    Everything is an ordinary Dry record: visible in the web app, searchable, shareable, exportable — the agent's
    memory is never a hidden file.
  · GENERAL PLUGIN (plugins.enabled: [dry]) — the /dry slash command and two skills (dry:using-dry, dry:memories).
Building on Dry itself (spaces, types, records, pages) goes through Dry's MCP server, which Hermes connects to separately
(`hermes mcp add dry --url https://dry.ai/api/mcp --auth oauth`); this plugin does not duplicate those tools.
Credentials: DRY_TOKEN (a personal access token from Dry: Account → Agents & tokens), read with get_secret, never os.environ.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, is_trivial_prompt, spawn_context_thread
from agent.secret_scope import get_secret

from . import awareness
from .dry_client import DryClient, DryError, field_value

logger = logging.getLogger(__name__)
NAME = "dry"
DEFAULT_URL = "https://dry.ai"
CONFIG_FILE = "dry.json"        # non-secret settings, in the profile's HERMES_HOME
MAP_FILE = "dry-memory-map.json"  # which Memory record mirrors which built-in memory entry (by content hash)
SKILLS = Path(__file__).parent / "skills"
RECALL_LIMIT = 5
NOTE_CLIP = 280
CHANGE_POLL_S = 30          # the agent re-checks the change feed at most this often (it is per turn, not a timer)
SESSION_NOTES_MIN_TURNS = 4
_CTX: Any = None            # the PluginContext from register() in THIS module copy

# Hermes imports a dual-kind plugin twice (the general loader and the memory loader each get their own module copy), so
# what both copies need — the real PluginContext and the one watcher per Hermes home — lives in one holder in sys.modules.
import sys as _sys
import types as _types
_SHARED = _sys.modules.setdefault("_dry_hermes_shared", _types.ModuleType("_dry_hermes_shared"))
if not hasattr(_SHARED, "ctx"):
    _SHARED.ctx, _SHARED.watchers = None, {}


def _llm() -> Any:
    """Completions on the person's active model: the general plugin context's ctx.llm, else the same facade directly."""
    ctx = _SHARED.ctx or _CTX
    llm = getattr(ctx, "llm", None) if ctx is not None else None
    if llm is not None:
        return llm
    try:
        from agent.plugin_llm import PluginLlm
        return PluginLlm(plugin_id=getattr(ctx, "plugin_id", None) or NAME)
    except Exception:
        return None


# ---- settings ------------------------------------------------------------------------------------------------
def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        from utils import atomic_json_write
        atomic_json_write(path, data)
    except ImportError:
        tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(data, indent=2)); tmp.replace(path)


def _home(hermes_home: Optional[str] = None) -> Path:
    if hermes_home:
        return Path(hermes_home)
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def load_settings(hermes_home: Optional[str] = None) -> dict:
    s = _read_json(_home(hermes_home) / CONFIG_FILE)
    return {"url": (s.get("url") or get_secret("DRY_URL", "") or DEFAULT_URL).rstrip("/"), "space": s.get("space") or "",
            "recall_limit": int(s.get("recall_limit") or RECALL_LIMIT),
            "watch": s.get("watch") or "all",                    # "all" = every space you belong to, or a list of names/ids
            "notify_to": s.get("notify_to") or "",               # live alerts target for send_message: telegram · discord · platform:chat_id …
            "notify_own": bool(s.get("notify_own", False)),      # alert about your own edits too (off: you know what you changed)
            "changes": s.get("changes", True) is not False,      # tell the agent what changed since it last looked
            "session_notes": s.get("session_notes", True) is not False}


def save_settings(values: dict, hermes_home: Optional[str] = None) -> dict:
    path = _home(hermes_home) / CONFIG_FILE
    cur = _read_json(path)
    cur.update(values)
    _write_json(path, cur)
    return cur


def make_client(hermes_home: Optional[str] = None) -> Optional[DryClient]:
    token = (get_secret("DRY_TOKEN", "") or "").strip()
    return DryClient(load_settings(hermes_home)["url"], token) if token else None


def _now_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _title_from(text: str) -> str:
    line = next((l.strip(" -*#\t") for l in (text or "").splitlines() if l.strip()), "Memory")
    return line if len(line) <= 90 else line[:87].rstrip() + "…"


def _clip(text: Any, n: int = NOTE_CLIP) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode()).hexdigest()[:24]


# ---- the provider ----------------------------------------------------------------------------------------------
class DryMemoryProvider(MemoryProvider):
    """The person's Dry Memories space as Hermes's long-term memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, dict] = {}   # per HERMES_HOME (one process may serve several profiles)
        self._writer: Optional[threading.Thread] = None

    # -- identity / availability
    @property
    def name(self) -> str:
        return NAME

    def is_available(self) -> bool:            # no network here, by contract
        return bool((get_secret("DRY_TOKEN", "") or "").strip())

    def unavailable_reason(self) -> str:
        return "DRY_TOKEN is not set — run `hermes memory setup` and paste a Dry personal access token (Dry: Account → Agents & tokens)."

    # -- per-profile state
    def _st(self, hermes_home: Optional[str] = None) -> dict:
        key = str(_home(hermes_home))
        with self._lock:
            st = self._state.setdefault(key, {"home": key, "client": None, "space": None, "type": None, "context": "primary", "error": "",
                                              "me": None, "last_poll": 0.0, "greeted": False, "profile": None, "session": "", "title": ""})
        if st["client"] is None:
            st["client"] = make_client(key)
        return st

    def _ensure_space(self, st: dict) -> tuple[DryClient, dict, dict]:
        client = st["client"]
        if client is None:
            raise DryError(0, self.unavailable_reason())
        if st["me"] is None:
            st["me"] = client.me().get("userId")
        if st["space"] is None:
            space, mtype = client.memories(load_settings(st["home"])["space"])
            st["space"], st["type"] = space, mtype
        return client, st["space"], st["type"]

    def initialize(self, session_id: str, **kwargs) -> None:
        st = self._st(kwargs.get("hermes_home"))
        st["context"] = kwargs.get("agent_context") or "primary"
        if st.get("session") != session_id:
            st["greeted"], st["profile"], st["last_poll"] = False, None, 0.0
        st["session"] = session_id
        st["title"] = kwargs.get("session_title") or ""
        # resolve the space in the background so the first turn never waits on the network
        def _warm():
            try:
                self._ensure_space(st)
                st["error"] = ""
            except Exception as e:  # reported by recall / status, never raised into the agent
                st["error"] = str(e)
                logger.warning("dry: could not open the Memories space: %s", e)
        spawn_context_thread(_warm, name="dry-warm").start()

    # -- what the model is told once
    def system_prompt_block(self) -> str:
        st = self._st()
        where = ""
        if st.get("space") and st.get("client"):
            where = f" ({st['client'].space_url(st['space']['id'])})"
        return (
            "## Long-term memory: Dry\n"
            f"Your long-term memory is the person's Dry space \"{(st.get('space') or {}).get('name', 'Memories')}\"{where}. "
            "Memories relevant to each message are added to it automatically under \"From your Dry memories\".\n"
            "- Save durable facts, preferences and decisions with `dry_remember` (a short Title, the detail in Note). Never save secrets.\n"
            "- Look things up with `dry_recall`; `all_spaces: true` searches every Dry space the person can see.\n"
            "- `dry_forget` removes a memory when the person asks.\n"
            "- What changed in their Dry spaces since you last looked (by them in the web app, or by other people) is added under "
            "\"Changes in Dry\"; `dry_changes` lists recent changes on demand. Mention a change when it matters to what they ask.\n"
            "- Every memory is a Dry record with a link; give the person the link when you save or find one.\n"
            "- To build in Dry (spaces, types, records, pages) use Dry's MCP tools; read the skill `dry:using-dry` first "
            "(`skill_view(\"dry:using-dry\")`). How memory works here: `dry:memories`."
        ) + self._profile_block(st)

    def _profile_block(self, st: dict) -> str:
        """The person's "About the person" memories — edited in Dry, they change what the agent knows next session."""
        if st.get("profile") is None:
            st["profile"] = []
            try:
                client, space, mtype = self._ensure_space(st)
                items = client.find_objects(space["id"], type_id=mtype["id"], search="About the person", mode="text", limit=40)
                st["profile"] = [field_value(o, mtype, "Note") or field_value(o, mtype, "Title") for o in items
                                 if str(field_value(o, mtype, "Title") or "").startswith("About the person")]
            except Exception as e:
                logger.debug("dry: profile not loaded: %s", e)
        facts = [ _clip(str(f).replace("About the person: ", ""), 200) for f in st["profile"] if f][:25]
        return ("\n\n## What you know about the person (from Dry; they can edit it there)\n" + "\n".join(f"- {f}" for f in facts)) if facts else ""

    # -- recall before each turn
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        st = self._st()
        changes = self._changes_block(st)
        if not query or is_trivial_prompt(query):
            return changes
        mem = self._memories_block(st, query)
        return "\n\n".join(b for b in (changes, mem) if b)

    def _changes_block(self, st: dict) -> str:
        """What changed in the person's spaces since the agent last looked — polled at most every CHANGE_POLL_S, per turn."""
        if st.get("client") is None or st.get("context", "primary") != "primary":
            return ""
        settings = load_settings(st["home"])
        if not settings["changes"] or time.time() - st.get("last_poll", 0) < CHANGE_POLL_S:
            return ""
        st["last_poll"] = time.time()
        try:
            client, _, _ = self._ensure_space(st)
            items = awareness.collect(client, st["home"], "agent", awareness.watched_spaces(client, settings["watch"]), st["me"])
        except Exception as e:
            logger.debug("dry: change check failed: %s", e)
            return ""
        header = "## Changed in Dry just now" if st.get("greeted") else "## Changes in Dry since we last talked"
        st["greeted"] = True
        return awareness.digest(items, header)

    def _memories_block(self, st: dict, query: str) -> str:
        try:
            client, space, mtype = self._ensure_space(st)
            items = client.search(space["id"], query, type_id=mtype["id"], limit=load_settings(st["home"])["recall_limit"])
        except Exception as e:
            logger.debug("dry: recall failed: %s", e)
            return ""
        if not items:
            return ""
        lines = ["## From your Dry memories"]
        for o in items:
            title = field_value(o, mtype, "Title") or o.get("title") or "Memory"
            note = field_value(o, mtype, "Note")
            when = field_value(o, mtype, "When")
            lines.append(f"- **{title}**" + (f" ({str(when)[:10]})" if when else "") + (f" — {_clip(note)}" if note else "") + f" · {client.record_url(o['id'])}")
        return "\n".join(lines)

    # -- turns are NOT stored: a person's Memories space is for things worth remembering, not chat logs
    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        return None

    # -- mirror Hermes's own memory writes
    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        st = self._st()
        if st.get("context", "primary") != "primary" or st.get("client") is None:
            return
        prev = (metadata or {}).get("previous_content")

        def _mirror():
            try:
                self._mirror(st, action, target, content, prev)
            except Exception as e:
                logger.warning("dry: mirroring a memory %s failed: %s", action, e)
        if self._writer and self._writer.is_alive():
            self._writer.join(timeout=5.0)
        self._writer = spawn_context_thread(_mirror, name="dry-mirror")
        self._writer.start()

    def _mirror(self, st: dict, action: str, target: str, content: str, prev: Optional[str]) -> None:
        client, space, mtype = self._ensure_space(st)
        path = Path(st["home"]) / MAP_FILE
        mapping = _read_json(path)
        about = "About the person: " if target == "user" else ""
        values = lambda text: {"Title": about + _title_from(text), "Note": text, "When": _now_date()}
        if action == "add":
            o = client.create_object(space["id"], mtype["id"], values(content))
            awareness.mark_own(st["home"], [o["id"]])
            mapping[_hash(content)] = o["id"]
        elif action == "replace":
            oid = mapping.pop(_hash(prev), None) if prev else None
            if oid:
                try:
                    awareness.mark_own(st["home"], [oid])
                    client.update_object(space["id"], oid, values(content))
                except DryError as e:
                    if e.status != 404:
                        raise
                    oid = None
            if not oid:
                oid = client.create_object(space["id"], mtype["id"], values(content))["id"]
                awareness.mark_own(st["home"], [oid])
            mapping[_hash(content)] = oid
        elif action == "remove":
            oid = mapping.pop(_hash(prev or content), None)
            if oid:   # only a record this plugin wrote; an older entry it never mirrored is left alone
                awareness.mark_own(st["home"], [oid])
                try:
                    client.delete_object(space["id"], oid)
                except DryError as e:
                    if e.status != 404:
                        raise
        _write_json(path, mapping)

    # -- tools
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {"name": "dry_remember", "description": "Save something worth remembering to the person's Dry Memories space (a Dry record they can see, search and share). Use a short Title; put the detail in Note. Never store secrets. Returns the record's link.",
             "parameters": {"type": "object", "properties": {
                 "title": {"type": "string", "description": "A short headline, e.g. 'Prefers metric units'"},
                 "note": {"type": "string", "description": "The detail, in plain words"},
                 "source": {"type": "string", "description": "Optional web address this came from"}}, "required": ["title"]}},
            {"name": "dry_recall", "description": "Search the person's Dry memories by words and meaning. Set all_spaces to true to search every Dry space they can see (records, pages, notes). Returns titles, short text and links.",
             "parameters": {"type": "object", "properties": {
                 "query": {"type": "string"},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                 "all_spaces": {"type": "boolean", "default": False}}, "required": ["query"]}},
            {"name": "dry_changes", "description": "What changed in the person's Dry spaces recently — by them (web app, phone) or by other people; the agent's own writes are left out. Use when they ask what's new, or before acting on a space others edit.",
             "parameters": {"type": "object", "properties": {
                 "hours": {"type": "number", "default": 24, "description": "How far back"},
                 "space": {"type": "string", "description": "Only this space (name or id)"},
                 "others_only": {"type": "boolean", "default": False, "description": "Only changes by other people"}}}},
            {"name": "dry_forget", "description": "Delete one memory from the person's Dry Memories space, by its link or id (from dry_recall). Only when the person asks to forget it.",
             "parameters": {"type": "object", "properties": {"memory": {"type": "string", "description": "The memory's Dry link or id"}}, "required": ["memory"]}},
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            st = self._st()
            client, space, mtype = self._ensure_space(st)
            if tool_name == "dry_remember":
                title = str(args.get("title") or "").strip()
                if not title:
                    return json.dumps({"ok": False, "error": "title is required"})
                values = {"Title": title[:200], "When": _now_date()}
                if args.get("note"):
                    values["Note"] = str(args["note"])
                if args.get("source"):
                    values["Source"] = str(args["source"])
                o = client.create_object(space["id"], mtype["id"], values)
                awareness.mark_own(st["home"], [o["id"]])
                return json.dumps({"ok": True, "id": o["id"], "url": client.record_url(o["id"]), "space": space.get("name")})
            if tool_name == "dry_recall":
                q = str(args.get("query") or "").strip()
                limit = max(1, min(int(args.get("limit") or 5), 20))
                if not q:
                    return json.dumps({"ok": False, "error": "query is required"})
                if args.get("all_spaces"):
                    return json.dumps({"ok": True, "results": self._search_all(client, q, limit, first=space)})
                items = client.search(space["id"], q, type_id=mtype["id"], limit=limit)
                return json.dumps({"ok": True, "results": [{"id": o["id"], "title": field_value(o, mtype, "Title"), "note": _clip(field_value(o, mtype, "Note"), 600),
                                                             "when": field_value(o, mtype, "When"), "url": client.record_url(o["id"])} for o in items]})
            if tool_name == "dry_forget":
                ref = str(args.get("memory") or "").strip()
                found = client.locate(ref)
                if found.get("spaceId") != space["id"]:
                    return json.dumps({"ok": False, "error": "That record is not in the Memories space; dry_forget only removes memories."})
                awareness.mark_own(st["home"], [found["id"]])
                client.delete_object(space["id"], found["id"])
                return json.dumps({"ok": True, "deleted": found["id"]})
            if tool_name == "dry_changes":
                hours = float(args.get("hours") or 24)
                try:
                    awareness.collect(client, st["home"], "agent", awareness.watched_spaces(client, load_settings(st["home"])["watch"]), st["me"])
                    st["last_poll"] = time.time()
                except DryError:
                    pass
                items = awareness.recent(st["home"], hours, str(args.get("space") or ""))
                if args.get("others_only"):
                    items = [i for i in items if not i.get("actorIsMe")]
                return json.dumps({"ok": True, "hours": hours, "changes": [{"when": i.get("at"), "space": i.get("space"), "what": awareness.sentence(i)} for i in items[-50:]]})
            return json.dumps({"ok": False, "error": f"unknown tool {tool_name}"})
        except DryError as e:
            return json.dumps({"ok": False, "error": str(e)})
        except Exception as e:  # a tool never raises into the agent
            logger.warning("dry: %s failed: %s", tool_name, e)
            return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _search_all(self, client: DryClient, q: str, limit: int, first: Optional[dict] = None) -> list:
        """Memories first, then every other space the person can see (up to 25), each answer with its space, type and link."""
        spaces = client.spaces()
        if first:
            spaces = [first] + [s for s in spaces if s.get("id") != first.get("id")]
        out: list = []
        for s in spaces[:25]:
            try:
                types = {t["id"]: t for t in client.types(s["id"])}
                for o in client.search(s["id"], q, limit=limit):
                    t = types.get(o.get("typeId")) or {}
                    title = field_value(o, t, "Title") or o.get("title") or next((v for v in (o.get("values") or {}).values() if isinstance(v, str)), "")
                    out.append({"space": s.get("name"), "type": t.get("name"), "title": _clip(title, 120), "id": o["id"], "url": client.record_url(o["id"])})
            except DryError:
                continue
            if len(out) >= limit * 2:
                break
        return out[: limit * 2]

    # -- setup / status
    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "token", "description": "Dry personal access token (Dry → Account → Agents & tokens → Create)", "secret": True, "required": True,
             "env_var": "DRY_TOKEN", "url": "https://dry.ai/account"},
            {"key": "url", "description": "Your Dry address", "default": DEFAULT_URL},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        save_settings({k: v for k, v in values.items() if k in ("url", "space", "recall_limit", "watch", "notify_to", "notify_own", "changes", "session_notes") and v not in (None, "")}, hermes_home)

    def post_setup(self, hermes_home: str, config: dict) -> None:
        """`hermes memory setup` → dry hands the whole setup to us: sign in with the browser, activate, connect Dry's tools,
        choose where live alerts go. Nothing to copy, no file to edit."""
        from .setup_wizard import run_setup
        run_setup(hermes_home, config)

    def get_status_config(self, provider_config: dict) -> dict:
        return {"summary": status_summary()}

    # -- session notes: what a conversation settled, kept as ONE memory per session (upserted), never the transcript
    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        self._session_notes(messages, final=True)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        self._session_notes(messages, final=False)   # before the context is summarised away
        return ""

    def _session_notes(self, messages: List[Dict[str, Any]], *, final: bool) -> None:
        st = self._st()
        if st.get("context", "primary") != "primary" or st.get("client") is None or not load_settings(st["home"])["session_notes"]:
            return
        talk = [(m.get("role"), _text_of(m.get("content"))) for m in (messages or []) if m.get("role") in ("user", "assistant")]
        talk = [(r, t) for r, t in talk if t.strip()]
        if sum(1 for r, _ in talk if r == "user") < SESSION_NOTES_MIN_TURNS:
            return
        notes = summarise_session(talk)
        if not notes:
            return

        def _save():
            try:
                client, space, mtype = self._ensure_space(st)
                path = Path(st["home"]) / MAP_FILE
                mapping = _read_json(path)
                key = f"session:{st.get('session') or 'unknown'}"
                title = f"Session notes — {_now_date()}" + (f" · {st['title']}" if st.get("title") else "")
                values = {"Title": title[:200], "Note": notes, "When": _now_date()}
                oid = mapping.get(key)
                if oid:
                    try:
                        awareness.mark_own(st["home"], [oid])
                        client.update_object(space["id"], oid, values)
                    except DryError as e:
                        if e.status != 404:
                            raise
                        oid = None
                if not oid:
                    oid = client.create_object(space["id"], mtype["id"], values)["id"]
                    awareness.mark_own(st["home"], [oid])
                mapping[key] = oid
                _write_json(path, mapping)
            except Exception as e:
                logger.warning("dry: saving session notes failed: %s", e)
        t = spawn_context_thread(_save, name="dry-session-notes")
        t.start()
        if final:
            t.join(timeout=20.0)   # the process may be exiting

    def shutdown(self) -> None:
        if self._writer and self._writer.is_alive():
            self._writer.join(timeout=5.0)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


NOTES_PROMPT = ("You keep a person's long-term memory. From the conversation below, write at most 6 short bullet points "
                "('- ') worth remembering weeks from now: decisions and their reasons, preferences, facts about people, projects "
                "and places, commitments with dates, open threads. Plain words, no chit-chat, no tool or system details, never "
                "passwords, tokens or keys. If nothing is worth remembering, reply exactly NONE.")


def summarise_session(talk: List[tuple]) -> str:
    """The conversation's lasting points, on the person's own active model (ctx.llm). '' when nothing lasts or no model."""
    llm = _llm()
    if llm is None:
        return ""
    text, total = [], 0
    for role, t in reversed(talk):                      # newest first, ~12k characters
        piece = f"{'Person' if role == 'user' else 'Assistant'}: {_clip(t, 1500)}"
        total += len(piece)
        if total > 12000:
            break
        text.append(piece)
    out, err = "", None
    for attempt in (1, 2):                              # one retry: a dropped provider call must not lose a session's notes
        try:
            r = llm.complete([{"role": "system", "content": NOTES_PROMPT}, {"role": "user", "content": "\n\n".join(reversed(text))}],
                             max_tokens=400, temperature=0.2, timeout=60, purpose="dry: session notes")
            out = (getattr(r, "text", "") or "").strip()
            if out:
                break
        except Exception as e:
            err = e
    if not out:
        logger.warning("dry: session notes not written — the model call failed twice: %s", err or "empty answer")
        return ""
    if not out or out.upper().startswith("NONE"):
        return ""
    lines = [l.strip() for l in out.splitlines() if l.strip().startswith(("-", "•", "*"))]
    return "\n".join("- " + l.lstrip("-•* ").strip() for l in lines[:6]) if lines else _clip(out, 1200)


def status_summary(hermes_home: Optional[str] = None) -> str:
    client = make_client(hermes_home)
    if client is None:
        return "Dry: no token — run `hermes memory setup` (Dry: Account → Agents & tokens)."
    try:
        me = client.me()
        space, mtype = client.memories(load_settings(hermes_home)["space"])
        count = len(client.find_objects(space["id"], type_id=mtype["id"], limit=200))
        return f"Dry: signed in as {me.get('email') or me.get('displayName') or me.get('userId')} on {client.web_origin()} · memories: {count}{'+' if count >= 200 else ''} in \"{space.get('name')}\" · {client.space_url(space['id'])}"
    except DryError as e:
        return f"Dry: {e}"


# ---- the /dry slash command -------------------------------------------------------------------------------------
HELP = ("/dry — your Dry memory\n"
        "  /dry                 where your memories live\n"
        "  /dry find <words>    search your memories (/dry find --all <words>: every space)\n"
        "  /dry save <text>     save a memory (first line = title)\n"
        "  /dry spaces          your Dry spaces, with links\n"
        "  /dry changes [hours] what changed in your spaces (default: 24 h)\n"
        "  /dry watch on <target> | off | status   live alerts when others change your spaces\n"
        "                       target = where Hermes sends them: telegram · discord · signal · platform:chat_id\n"
        "  /dry status          account, address, memory count")


def dry_command(raw_args: str) -> str:
    args = (raw_args or "").strip()
    verb, _, rest = args.partition(" ")
    verb, rest = verb.lower(), rest.strip()
    client = make_client()
    if client is None:
        return "Dry is not set up: run `hermes memory setup` and choose dry (needs a Dry personal access token)."
    p = DryMemoryProvider()
    try:
        if verb in ("", "open"):
            space, _ = client.memories(load_settings()["space"])
            return f"Your Dry memories: {client.space_url(space['id'])}\n\n{HELP}"
        if verb in ("help", "-h", "--help"):
            return HELP
        if verb == "status":
            return status_summary()
        if verb == "spaces":
            rows = [f"- {s.get('name')} · {', '.join(s.get('roles') or [])} · {client.space_url(s['id'])}" for s in client.spaces()]
            return "Your Dry spaces:\n" + ("\n".join(rows) or "(none)")
        if verb in ("find", "search", "recall"):
            all_spaces = rest.startswith("--all")
            q = rest[5:].strip() if all_spaces else rest
            if not q:
                return "Usage: /dry find <words>"
            r = json.loads(p.handle_tool_call("dry_recall", {"query": q, "limit": 8, "all_spaces": all_spaces}))
            if not r.get("ok"):
                return f"Dry: {r.get('error')}"
            res = r["results"]
            if not res:
                return f"Nothing in Dry matches \"{q}\"."
            return "\n".join(f"- {x.get('title') or '(untitled)'}" + (f" [{x['space']} · {x.get('type')}]" if all_spaces else "") + f" · {x['url']}" for x in res)
        if verb == "changes":
            hours = float(rest) if rest.replace(".", "", 1).isdigit() else 24.0
            r = json.loads(p.handle_tool_call("dry_changes", {"hours": hours}))
            if not r.get("ok"):
                return f"Dry: {r.get('error')}"
            ch = r["changes"]
            return (f"Changes in Dry, last {hours:g} h:\n" + "\n".join(f"- [{c['space']}] {c['what']}" for c in ch)) if ch else f"No changes in your Dry spaces in the last {hours:g} h (that the agent did not make itself)."
        if verb == "watch":
            return watch_command(rest)
        if verb in ("save", "remember", "add"):
            if not rest:
                return "Usage: /dry save <text>"
            title, _, note = rest.partition("\n")
            r = json.loads(p.handle_tool_call("dry_remember", {"title": _title_from(title), "note": note.strip() or (rest if len(rest) > 90 else "")}))
            return f"Saved to Dry: {r['url']}" if r.get("ok") else f"Dry: {r.get('error')}"
        return HELP
    except DryError as e:
        return f"Dry: {e}"


# ---- entry point (both loaders call it) --------------------------------------------------------------------------
def register(ctx) -> None:
    ctx.register_memory_provider(DryMemoryProvider())
    for name, desc in (("using-dry", "How to build and find things in Dry well: spaces, types, records, pages, links"),
                       ("memories", "How Dry works as your long-term memory: what to save, recall, forget")):
        try:
            ctx.register_skill(name, SKILLS / name / "SKILL.md", desc)
        except Exception as e:  # an older Hermes without plugin skills still gets the memory
            logger.debug("dry: skill %s not registered: %s", name, e)
    if hasattr(ctx, "register_command"):
        ctx.register_command("dry", dry_command, description="Your Dry memory: find, save, changes, watch, spaces, status", args_hint="[find|save|changes|watch|spaces|status] <text>")
    global _CTX
    _CTX = ctx
    if hasattr(ctx, "dispatch_tool") and hasattr(ctx, "register_tool"):   # the real PluginContext (general loader)
        _SHARED.ctx = ctx
    if hasattr(ctx, "register_hook"):
        try:
            ctx.register_hook("post_tool_call", _on_post_tool_call)
        except Exception as e:
            logger.debug("dry: post_tool_call hook not registered: %s", e)
    _maybe_watch_in_gateway()


# ---- the agent's own writes through Dry's MCP tools (so they are never reported back as the person's changes) ----
_WRITE_WORDS = ("create", "update", "delete", "edit_page", "import", "upload", "add_member", "remove_member", "clone", "comment")


def _on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, **kwargs) -> None:
    name = (tool_name or "").lower()
    if "dry" not in name or not any(w in name for w in _WRITE_WORDS):
        return
    try:
        awareness.mark_own(str(_home()), awareness.ids_in(args, result))
    except Exception as e:
        logger.debug("dry: could not record an own write: %s", e)


# ---- live alerts: SSE in the gateway ------------------------------------------------------------------------------
def _hermes_bin() -> str:
    import shutil
    for cand in (shutil.which("hermes"), str(Path.home() / ".local/bin/hermes")):
        if cand and Path(cand).exists():
            return cand
    return "hermes"


def _send(target: str, text: str, hermes_home: Optional[str] = None) -> dict:
    """Deliver an alert through `hermes send` — Hermes's public command for a script or plugin to message the person on a
    platform the gateway is configured for (send_message is deliberately not an agent tool). Plain text, no model call."""
    import os
    import subprocess
    env = dict(os.environ, HERMES_HOME=str(_home(hermes_home)))
    try:
        p = subprocess.run([_hermes_bin(), "send", "-t", target, "--json", "-f", "-"], input=text, text=True,
                           capture_output=True, timeout=90, env=env)
    except Exception as e:
        logger.warning("dry: alert to %s not sent: %s", target, e)
        return {"ok": False, "error": str(e)}
    try:
        j = json.loads((p.stdout or "").strip() or "{}")       # --json prints one (pretty-printed) object
    except ValueError:
        j = {}
    ok = p.returncode == 0 and not j.get("error") and j.get("success", True) is not False
    if not ok:
        logger.warning("dry: alert to %s not sent (exit %s): %s", target, p.returncode, j.get("error") or (p.stderr or p.stdout or "").strip()[-300:])
    return {"ok": ok, **({"result": j} if j else {})}


def _make_watcher(home: str) -> "awareness.Watcher":
    client = make_client(home)
    me = client.me().get("userId")
    return awareness.Watcher(client, home, me, lambda: load_settings(home), lambda t, m: _send(t, m, home), spawn_context_thread)


def _maybe_watch_in_gateway() -> None:
    """Start the SSE watcher once per Hermes home, only inside the gateway process, only when alerts are switched on."""
    home = str(_home())

    def _go():
        if not awareness.in_gateway(home, wait_s=120):
            return
        if not load_settings(home)["notify_to"] or make_client(home) is None:
            return
        try:
            awareness.start_watcher(home, lambda: _make_watcher(home))
        except Exception as e:
            logger.warning("dry: live alerts not started: %s", e)
    try:
        spawn_context_thread(_go, name="dry-watch-start").start()
    except Exception as e:
        logger.debug("dry: watcher start skipped: %s", e)


def watch_command(rest: str) -> str:
    home = str(_home())
    verb, _, target = (rest or "").strip().partition(" ")
    verb, target = verb.lower(), target.strip()
    st = load_settings(home)
    w = awareness.watcher_for(home)
    if verb in ("", "status"):
        on = f"ON → {st['notify_to']}" if st["notify_to"] else "OFF"
        live = "running in this gateway" if w else ("will start with the gateway" if st["notify_to"] else "not running")
        return (f"Live Dry alerts: {on} ({live}). Watching: {st['watch'] if st['watch'] != 'all' else 'every space you belong to'}."
                + (f" Sent this run: {len(w.sent)}." if w else ""))
    if verb == "off":
        save_settings({"notify_to": ""}, home)
        if w:
            w.stop.set()
            awareness._watchers.pop(home, None)
        return "Live Dry alerts are off."
    if verb == "on":
        if not target:
            return "Usage: /dry watch on <target> — e.g. telegram (your home channel), discord, signal, or platform:chat_id"
        save_settings({"notify_to": target}, home)
        if awareness.in_gateway(home):
            try:
                awareness.start_watcher(home, lambda: _make_watcher(home))
                return f"Live Dry alerts are on → {target}. Changes others make in your spaces arrive within seconds."
            except Exception as e:
                return f"Saved, but the watcher did not start: {e}"
        return f"Live Dry alerts are on → {target}. They run inside the Hermes gateway: start or restart it (`hermes gateway restart`)."
    return "Usage: /dry watch [on <target> | off | status]"
