"""A small, dependency-free client for Dry's REST API (the same API the web app and the MCP tools use).

Every call acts as the person whose personal access token it carries — the same rights as in the web app.
Errors become DryError with Dry's own message; nothing here retries or caches across processes.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

USER_AGENT = "Mozilla/5.0 (compatible; dry-hermes/0.1; +https://dry.ai/mcp)"
MEMORIES_SPACE = "Memories"
MEMORY_TYPE = "Memory"
# the Memory type exactly as Dry gives every new account — used only when the space is missing
MEMORY_TYPE_BODY = {"name": MEMORY_TYPE, "description": "Something worth remembering — a note, a fact, a thought", "fields": [
    {"label": "Title", "kind": "text", "mode": "required"},
    {"label": "Note", "kind": "textarea", "mode": "optional"},
    {"label": "When", "kind": "date", "mode": "optional", "default": "now"},
    {"label": "Source", "kind": "url", "mode": "optional"},
]}


class DryError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Dry answered {status}: {message}" if status else message)
        self.status = status


class DryClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10.0):
        self.base = (base_url or "https://dry.ai").rstrip("/")
        self.token = token
        self.timeout = timeout
        self._web_origin: Optional[str] = None

    # ---- transport -------------------------------------------------------------------------------------------
    def request(self, method: str, path: str, body: Any = None, query: Optional[dict] = None, *, auth: bool = True, timeout: Optional[float] = None) -> Any:
        url = f"{self.base}/api{path}"
        if query:
            q = {k: v for k, v in query.items() if v is not None and v != ""}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, method=method, headers=headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                j = json.loads(e.read() or b"{}")
                msg = j.get("error") or j.get("message") or ""
            except Exception:
                pass
            raise DryError(e.code, msg or e.reason) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise DryError(0, f"Cannot reach Dry at {self.base}: {getattr(e, 'reason', e)}") from None

    # ---- addresses -------------------------------------------------------------------------------------------
    def web_origin(self) -> str:
        """The address people open (links), from Dry's public config; falls back to the API's own origin."""
        if self._web_origin is None:
            try:
                self._web_origin = (self.request("GET", "/config", auth=False, timeout=5) or {}).get("webOrigin") or self.base
            except DryError:
                self._web_origin = self.base
            self._web_origin = self._web_origin.rstrip("/")
        return self._web_origin

    def record_url(self, object_id: str) -> str:
        return f"{self.web_origin()}/o/{short_id(object_id)}"

    def space_url(self, space_id: str) -> str:
        return f"{self.web_origin()}/s/{short_id(space_id)}"

    # ---- the calls the plugin makes --------------------------------------------------------------------------
    def me(self) -> dict:
        return self.request("GET", "/me")

    def spaces(self) -> list:
        r = self.request("GET", "/spaces")
        return r.get("items", r) if isinstance(r, dict) else (r or [])

    def types(self, space_id: str) -> list:
        r = self.request("GET", f"/spaces/{space_id}/types")
        return r.get("items", r) if isinstance(r, dict) else (r or [])

    def find_objects(self, space_id: str, *, type_id: Optional[str] = None, search: str = "", mode: str = "", limit: int = 10) -> list:
        q = {"typeId": type_id, "search": search, "mode": mode if search else "", "limit": max(1, min(int(limit), 200))}
        r = self.request("GET", f"/spaces/{space_id}/objects", query=q)
        return r.get("items", []) if isinstance(r, dict) else (r or [])

    def search(self, space_id: str, text: str, *, type_id: Optional[str] = None, limit: int = 5) -> list:
        """Hybrid (words + meaning) where the server offers it, plain text otherwise."""
        try:
            return self.find_objects(space_id, type_id=type_id, search=text, mode="hybrid", limit=limit)
        except DryError as e:
            if e.status == 400:
                return self.find_objects(space_id, type_id=type_id, search=text, mode="text", limit=limit)
            raise

    def create_object(self, space_id: str, type_id: str, values: dict) -> dict:
        return self.request("POST", f"/spaces/{space_id}/objects", {"typeId": type_id, "values": values})

    def update_object(self, space_id: str, object_id: str, values: dict) -> dict:
        return self.request("PATCH", f"/spaces/{space_id}/objects/{object_id}", {"values": values})

    def delete_object(self, space_id: str, object_id: str) -> None:
        self.request("DELETE", f"/spaces/{space_id}/objects/{object_id}")

    def locate(self, id_or_link: str) -> dict:
        """A record by its UUID, its short id or its Dry link → {id, spaceId, …}."""
        ref = (id_or_link or "").strip().rstrip("/")
        if "/" in ref:
            ref = ref.split("/")[-1].split("?")[0]
        return self.request("GET", f"/o/{urllib.parse.quote(ref)}")

    def get_object(self, space_id: str, object_id: str) -> dict:
        return self.request("GET", f"/spaces/{space_id}/objects/{object_id}")

    def changes(self, space_id: str, since: int = 0, limit: int = 1000) -> dict:
        """The space's change feed after `since`: {changes:[{seq,kind,op,id,spaceId,rev,actorId,at}], nextSince, hasMore}."""
        return self.request("GET", f"/spaces/{space_id}/changes", query={"since": since, "limit": limit})

    def latest_seq(self, space_id: str, since: int = 0, max_pages: int = 25) -> int:
        """Where the feed ends now (pages forward from `since`; a cursor is set once, so this is a one-time cost)."""
        for _ in range(max_pages):
            r = self.changes(space_id, since)
            since = r.get("nextSince", since)
            if not r.get("hasMore"):
                break
        return since

    def members(self, space_id: str) -> list:
        r = self.request("GET", f"/spaces/{space_id}/members")
        return r.get("members", []) if isinstance(r, dict) else (r or [])

    def stream_events(self, space_id: str, last_event_id: Optional[int] = None, *, read_timeout: float = 70.0):
        """SSE: yields each change event (dict) as Dry pushes it. Dry pings every 25 s, so a silent socket for
        `read_timeout` means the connection is dead → DryError, and the caller reconnects with the last seq it saw
        (Dry replays everything after Last-Event-ID, so nothing is missed)."""
        url = f"{self.base}/api/spaces/{space_id}/events"
        headers = {"Accept": "text/event-stream", "Authorization": f"Bearer {self.token}", "User-Agent": USER_AGENT, "Cache-Control": "no-cache"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=read_timeout)
        except urllib.error.HTTPError as e:
            raise DryError(e.code, e.reason) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise DryError(0, f"Cannot reach Dry at {self.base}: {getattr(e, 'reason', e)}") from None
        data, event = [], ""
        try:
            while True:
                try:
                    raw = resp.readline()
                except (TimeoutError, OSError) as e:
                    raise DryError(0, f"stream idle/closed: {e}") from None
                if not raw:
                    raise DryError(0, "stream closed by the server")
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    if data and event in ("", "change"):
                        try:
                            yield json.loads("\n".join(data))
                        except ValueError:
                            pass
                    data, event = [], ""
                elif line.startswith(":"):
                    continue                      # comment / ping
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
        finally:
            try:
                resp.close()
            except Exception:
                pass

    # ---- the Memories space ----------------------------------------------------------------------------------
    def memories(self, preferred: str = "") -> tuple[dict, dict]:
        """(space, Memory type) — the space named by `preferred` (id or name) or the person's own "Memories" space.
        Creates the space and/or the Memory type when missing (an account from before Memories existed)."""
        spaces = self.spaces()
        want = (preferred or MEMORIES_SPACE).strip()
        space = next((s for s in spaces if s.get("id") == want), None) or next(
            (s for s in spaces if (s.get("name") or "").lower() == want.lower() and "space_owner" in (s.get("roles") or [s.get("role")])), None)
        if space is None:
            space = self.request("POST", "/spaces", {"name": want if not _looks_like_id(want) else MEMORIES_SPACE, "public": False})
        mtype = next((t for t in self.types(space["id"]) if t.get("name") == MEMORY_TYPE), None)
        if mtype is None:
            mtype = self.request("POST", f"/spaces/{space['id']}/types", MEMORY_TYPE_BODY)
        return space, mtype


_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def short_id(uuid: str) -> str:
    """Dry's short id: the UUID's 128 bits in base62 (what Dry's own links use: /s/<short id>, /o/<short id>)."""
    try:
        n = int(uuid.replace("-", ""), 16)
    except (ValueError, AttributeError):
        return uuid
    out = ""
    while n:
        n, r = divmod(n, 62)
        out = _B62[r] + out
    return out.rjust(22, "0")


def _looks_like_id(s: str) -> bool:
    return len(s) == 36 and s.count("-") == 4


def field_value(obj: dict, mtype: dict, label: str) -> Any:
    """A record's value by its field LABEL (values are keyed by permanent field ids)."""
    for f in mtype.get("fields", []):
        if f.get("label") == label:
            return (obj.get("values") or {}).get(f.get("id"))
    return None
