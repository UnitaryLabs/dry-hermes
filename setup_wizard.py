"""The two-command setup:  hermes plugins install UnitaryLabs/dry-hermes --enable   →   hermes memory setup → dry.

Hermes hands the whole setup to the provider (post_setup): this wizard signs in with the browser (oauth_flow), makes dry the
memory provider, adds Dry's MCP tools with the same token (so the agent can also build spaces, types, records and pages),
asks where live alerts go, and ends with the status line. Every question has a default — Enter is always a safe answer —
and with no terminal attached it takes the defaults and asks nothing.
"""
from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
from typing import Optional

from .dry_client import DryClient, DryError

DEFAULT_URL = "https://dry.ai"


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(q: str, default: str = "") -> str:
    if not _tty():
        return default
    try:
        a = input(f"  {q}" + (f" [{default}]" if default else "") + ": ").strip()
    except EOFError:
        return default
    return a or default


def _yes(q: str, default: bool = True) -> bool:
    a = _ask(f"{q} ({'Y/n' if default else 'y/N'})", "").lower()
    return default if not a else a.startswith("y")


def _existing(base: str) -> Optional[dict]:
    tok = (os.environ.get("DRY_TOKEN") or "").strip()
    if not tok:
        return None
    try:
        return DryClient(base, tok).me()
    except DryError:
        return None


def _dry_mcp_entry(config: dict, base: str) -> Optional[str]:
    """The name of an MCP server already pointing at this Dry (any auth), if there is one."""
    host = base.split("://", 1)[-1].rstrip("/")
    for name, cfg in (config.get("mcp_servers") or {}).items():
        url = str((cfg or {}).get("url") or "")
        if host in url and "/mcp" in url:
            return name
    return None


def _platform_targets() -> list:
    """Messaging platforms Hermes can send to here (for the alerts question)."""
    from . import _hermes_bin
    try:
        p = subprocess.run([_hermes_bin(), "send", "--list", "--json"], capture_output=True, text=True, timeout=30)
        j = json.loads(p.stdout or "{}")
    except Exception:
        return []
    items = j.get("platforms") or j.get("targets") or j if isinstance(j, (dict, list)) else []
    names = []
    for it in (items if isinstance(items, list) else items.keys()):
        n = it.get("platform") or it.get("name") if isinstance(it, dict) else str(it)
        if n and n not in names:
            names.append(n)
    return names


def run_setup(hermes_home: str, config: dict, *, sign_in=None) -> dict:
    """Returns what it did (for tests): {"email", "base", "mcp", "alerts"}."""
    from . import load_settings, save_settings, status_summary
    from .oauth_flow import save_token, sign_in as browser_sign_in, who
    from hermes_cli.config import save_config
    sign_in = sign_in or browser_sign_in
    print("\n  ── Dry for Hermes ─────────────────────────────────────────────")
    print("  Dry becomes Hermes's long-term memory, and Hermes can build in your Dry spaces.\n")

    base = _ask("Your Dry address", load_settings(hermes_home)["url"] or DEFAULT_URL).rstrip("/")
    if "://" not in base:
        base = "https://" + base

    # 1. sign in (keep a working connection unless asked to replace it)
    me = _existing(base)
    if me and not _yes(f"Already connected as {me.get('email') or me.get('displayName')}. Connect again", False):
        pass
    else:
        me = None
        try:
            r = sign_in(base)
            save_token(r["token"], r["base"], hermes_home)
            me = who(r["base"], r["token"])
        except DryError as e:
            print(f"\n  ✗ {e}")
            if not _tty() or not _yes("Paste a Dry personal access token instead (Dry → Account → Agents & tokens → Create)", True):
                print("  Setup stopped. Run `hermes memory setup` again when ready.\n")
                return {"email": None}
            tok = getpass.getpass("  Token (starts with dry_pat_, hidden): ").strip()
            try:
                me = who(base, tok)
            except DryError as e2:
                print(f"  ✗ That token does not work: {e2}\n")
                return {"email": None}
            save_token(tok, base, hermes_home)
    save_settings({"url": base}, hermes_home)
    print(f"  ✓ Signed in to {base} as {me.get('email') or me.get('displayName')}")

    # 2. activate: dry as the memory provider, and the general plugin (command, skills, change hook) enabled
    if not isinstance(config.get("memory"), dict):
        config["memory"] = {}
    config["memory"]["provider"] = "dry"
    plugins = config.setdefault("plugins", {}) if isinstance(config.get("plugins", {}), dict) else {}
    enabled = plugins.setdefault("enabled", [])
    if isinstance(enabled, list) and "dry" not in enabled:
        enabled.append("dry")

    # 3. Dry's tools (build spaces, types, records, pages) with the same token — the token stays in .env
    mcp = _dry_mcp_entry(config, base)
    if mcp:
        print(f"  ✓ Dry's tools are already connected (MCP server '{mcp}')")
    elif _yes("Give Hermes Dry's tools too, so it can create spaces, types, records and pages", True):
        config.setdefault("mcp_servers", {})["dry"] = {"url": f"{base}/api/mcp", "headers": {"Authorization": "Bearer ${DRY_TOKEN}"}, "enabled": True}
        mcp = "dry"
        print("  ✓ Dry's tools added (MCP server 'dry', signed in with the same token)")
    save_config(config)
    print("  ✓ Dry is Hermes's memory provider")

    # 4. live alerts
    current = load_settings(hermes_home)["notify_to"]
    targets = _platform_targets()
    hint = f" — set up here: {', '.join(targets)}" if targets else " — e.g. telegram, discord, signal"
    where = _ask(f"Send live alerts when people change your Dry spaces? Where{hint} (none = off)", current or "none")
    where = "" if where.lower() in ("none", "no", "off", "-") else where
    save_settings({"notify_to": where}, hermes_home)
    print(f"  ✓ Live alerts: {'→ ' + where + ' (they run inside the gateway)' if where else 'off — turn on later with /dry watch on <target>'}")

    print("\n  " + status_summary(hermes_home))
    print("\n  Done. Start a new chat" + (" and restart the gateway (`hermes gateway restart`)" if where else "") + ".\n")
    return {"email": me.get("email"), "base": base, "mcp": mcp, "alerts": where}
