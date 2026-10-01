"""Sign in to Dry from Hermes with a browser — no token to copy.

Dry is its own OAuth 2.1 server for MCP clients (the same sign-in Claude.ai and ChatGPT use): this module discovers it,
registers "Hermes" as a client, opens Dry's Approve page in the browser, catches the answer on a one-time listener on
127.0.0.1 (PKCE), and exchanges the code for a token — a personal access token named "Hermes (MCP connector)" that the
person sees and can revoke in Dry under Account → Agents & tokens. The token is saved to the profile's .env as DRY_TOKEN
through Hermes's own writer.

Used by `hermes memory setup` (post_setup) and by Hermes Desktop's "Connect" button (start_loopback_flow_background /
get_flow_status — the contract hermes_cli/memory_oauth.py calls).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Optional

from .dry_client import USER_AGENT, DryClient, DryError

CLIENT_NAME = "Hermes"
DONE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Dry — connected</title><style>body{{margin:0;background:#262624;color:#faf9f5;
font:16px/1.5 system-ui,sans-serif}}main{{max-width:460px;margin:18vh auto;padding:28px;background:#1f1e1d;border:1px solid #3b3a36;border-radius:12px}}
h1{{font-size:20px;margin:0 0 8px}}p{{color:#c2c0b6}}</style></head><body><main><h1>{title}</h1><p>{text}</p></main></body></html>"""


def _json(url: str, body: Optional[dict] = None, form: Optional[dict] = None, timeout: float = 20) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    elif form is not None:
        data, headers["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read() or b"{}")
        except ValueError:
            j = {}
        raise DryError(e.code, j.get("error_description") or j.get("error") or e.reason) from None
    except (urllib.error.URLError, OSError) as e:
        raise DryError(0, f"Cannot reach Dry at {url.split('/.well-known')[0].split('/api/')[0]}: {getattr(e, 'reason', e)}") from None


def discover(base_url: str) -> dict:
    """Dry's authorization-server document (RFC 8414)."""
    base = base_url.rstrip("/")
    meta = _json(f"{base}/.well-known/oauth-authorization-server")
    if not meta.get("authorization_endpoint") or not meta.get("token_endpoint") or not meta.get("registration_endpoint"):
        raise DryError(0, f"{base} does not offer sign-in for MCP clients (no OAuth discovery document) — is it a Dry address?")
    return meta


class _Catcher(BaseHTTPRequestHandler):
    """Answers exactly one redirect from Dry on 127.0.0.1 and records code/state/error."""
    result: dict = {}

    def do_GET(self):  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if urllib.parse.urlparse(self.path).path != "/callback":
            self.send_response(404); self.end_headers(); return
        type(self).result.update({k: v[0] for k, v in q.items()})
        ok = "code" in q
        body = DONE_PAGE.format(title="Hermes is connected to Dry" if ok else "Not connected",
                                text="You can close this tab and go back to Hermes." if ok else "The sign-in was cancelled or refused. Go back to Hermes to try again.")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())
        type(self).done.set()

    def log_message(self, *a):   # keep the terminal clean
        pass


def sign_in(base_url: str, *, open_browser: bool = True, timeout: float = 600.0, say: Callable[[str], None] = print) -> dict:
    """Run the browser sign-in; returns {"token", "base"} — the access token is a Dry personal access token."""
    meta = discover(base_url)
    handler = type("Catcher", (_Catcher,), {"result": {}, "done": threading.Event()})
    server = HTTPServer(("127.0.0.1", 0), handler)
    redirect = f"http://127.0.0.1:{server.server_address[1]}/callback"
    try:
        reg = _json(meta["registration_endpoint"], {"client_name": CLIENT_NAME, "redirect_uris": [redirect],
                                                    "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                                                    "token_endpoint_auth_method": "none"})
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": reg["client_id"], "redirect_uri": redirect, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": state, "scope": "dry", "resource": f"{base_url.rstrip('/')}/api/mcp"})
        threading.Thread(target=server.serve_forever, name="dry-oauth-catcher", daemon=True).start()
        opened = False
        if open_browser:
            try:
                opened = webbrowser.open(url)
            except Exception:
                opened = False
        say(("  A browser window opened on Dry. Sign in if asked, then press \"Approve and connect\".\n" if opened else
             "  Open this link in a browser ON THIS COMPUTER, sign in to Dry, then press \"Approve and connect\":\n")
            + f"  {url}\n  Waiting for Dry… (up to {int(timeout // 60)} minutes; Ctrl+C to stop)")
        if not handler.done.wait(timeout):
            raise DryError(0, "No answer from the browser in time. Run the setup again when you are ready.")
        r = handler.result
        if r.get("error"):
            raise DryError(0, "The sign-in was cancelled in Dry." if r["error"] == "access_denied" else f"Dry refused the sign-in: {r.get('error_description') or r['error']}")
        if r.get("state") != state:
            raise DryError(0, "The answer did not match this sign-in (state mismatch). Run the setup again.")
        tok = _json(meta["token_endpoint"], form={"grant_type": "authorization_code", "code": r["code"], "code_verifier": verifier,
                                                  "client_id": reg["client_id"], "redirect_uri": redirect})
        if not tok.get("access_token"):
            raise DryError(0, "Dry did not return a token.")
        return {"token": tok["access_token"], "base": base_url.rstrip("/")}
    finally:
        server.shutdown()
        server.server_close()


def save_token(token: str, base_url: str, hermes_home: Optional[str] = None) -> None:
    """DRY_TOKEN (and DRY_URL when not the default) into the profile's .env — Hermes's own validated, 0600 writer."""
    from hermes_cli.config import save_env_value
    from contextlib import nullcontext
    ctx = nullcontext()
    if hermes_home:
        try:
            from hermes_constants import set_hermes_home_override, reset_hermes_home_override
            tok = set_hermes_home_override(str(hermes_home))
            class _Scope:
                def __enter__(self): return self
                def __exit__(self, *a): reset_hermes_home_override(tok)
            ctx = _Scope()
        except Exception:
            pass
    with ctx:
        save_env_value("DRY_TOKEN", token)
        if base_url.rstrip("/") != "https://dry.ai":
            save_env_value("DRY_URL", base_url.rstrip("/"))
    import os
    os.environ["DRY_TOKEN"] = token          # this process sees it at once (the wizard continues)
    if base_url.rstrip("/") != "https://dry.ai":
        os.environ["DRY_URL"] = base_url.rstrip("/")


def who(base_url: str, token: str) -> dict:
    return DryClient(base_url, token).me()


# ---- Hermes Desktop "Connect" button (hermes_cli/memory_oauth.py) --------------------------------------------------
_status = {"state": "idle", "detail": ""}
_thread: Optional[threading.Thread] = None


def start_loopback_flow_background(*, source: str = "hermes-desktop", timeout: float = 600.0, **_ignored) -> dict:
    global _thread
    if _status["state"] == "pending" and _thread and _thread.is_alive():
        return dict(_status)
    from . import load_settings, _home
    home = str(_home())
    base = load_settings(home)["url"]
    _status.update(state="pending", detail="waiting for approval in Dry")

    def _run():
        try:
            r = sign_in(base, timeout=timeout, say=lambda _m: None)
            save_token(r["token"], r["base"], home)
            me = who(r["base"], r["token"])
            _status.update(state="connected", detail=f"Connected as {me.get('email') or me.get('displayName') or 'you'}")
        except Exception as e:
            _status.update(state="error", detail=str(e))
    _thread = threading.Thread(target=_run, name="dry-oauth", daemon=True)
    _thread.start()
    return dict(_status)


def get_flow_status() -> dict:
    from . import make_client
    connected = False
    try:
        c = make_client()
        connected = bool(c and c.me().get("userId"))
    except Exception:
        pass
    return {**_status, "connected": connected, "auth": "oauth" if connected else ""}
