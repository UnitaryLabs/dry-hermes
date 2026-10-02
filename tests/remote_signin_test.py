"""Sign-in on a REMOTE machine (SSH · container · no display): setup says so up front and finishes from the address the browser
lands on, pasted back — against a running Dry, no browser involved (Dry's own Approve answers the address a browser would land on).

Run with a Dry whose dev profile accepts the x-dry-user header (local `pnpm dev`):
  DRY_TEST_URL=http://127.0.0.1:8800 DRY_TEST_USER=<a user id> python3 tests/remote_signin_test.py
Every token it makes is revoked again.
"""
import importlib.util
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dryplug", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(spec); sys.modules["dryplug"] = pkg   # the package shell only: oauth_flow + dry_client need no Hermes
of = importlib.import_module("dryplug.oauth_flow")

BASE = os.environ.get("DRY_TEST_URL", "http://127.0.0.1:8800").rstrip("/")
USER = os.environ.get("DRY_TEST_USER", "00000000-0000-7000-8002-00000000dead")
n = fails = 0
def check(name, cond, detail=""):
    global n, fails
    n += 1; fails += 0 if cond else 1
    print(f"{'✓' if cond else '✗'} {n}. {name}" + (f" — {detail}" if detail else ""))

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
opener = urllib.request.build_opener(NoRedirect)
def location(req) -> str:
    try:
        opener.open(req, timeout=20)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307): return e.headers["Location"]
        raise
    raise AssertionError("no redirect")

def approve(authorize_url: str) -> str:
    """What a person's browser does on another computer: open the link, sign in, press Approve — and where it then lands."""
    connect = location(urllib.request.Request(authorize_url))
    packed = urllib.parse.parse_qs(urllib.parse.urlparse(connect).query)["oauth"][0]
    body = urllib.parse.urlencode({"oauth": packed, "action": "approve"}).encode()
    return location(urllib.request.Request(f"{BASE}/api/oauth/approve", data=body, method="POST",
                                           headers={"content-type": "application/x-www-form-urlencoded", "x-dry-user": USER, "origin": BASE}))

def revoke(token: str):
    me = of.DryClient(BASE, token)
    tokens = me.request("GET", "/me/tokens") if hasattr(me, "request") else []
    for t in tokens if isinstance(tokens, list) else []:
        if token.startswith(t.get("prefix", "~")):
            urllib.request.urlopen(urllib.request.Request(f"{BASE}/api/me/tokens/{t['id']}", method="DELETE", headers={"x-dry-user": USER}), timeout=20)
            return True
    return False

def env(**kv):
    saved = {k: os.environ.get(k) for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "DISPLAY", "WAYLAND_DISPLAY", "DRY_SIGNIN", "WSL_DISTRO_NAME", "WSL_INTEROP")}
    for k in saved: os.environ.pop(k, None)
    os.environ.update({k: v for k, v in kv.items() if v is not None})
    return saved
def restore(saved):
    for k, v in saved.items():
        if v is None: os.environ.pop(k, None)
        else: os.environ[k] = v

# ---- 1. detection ---------------------------------------------------------------------------------------------------
container = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
s = env(SSH_CONNECTION="10.0.0.2 51000 10.0.0.9 22", DISPLAY=":0"); check("over SSH → remote, even with a display", "SSH" in (of.remote_reason() or ""), of.remote_reason()); restore(s)
s = env(DISPLAY=":0"); check("a desktop with a display → not remote", container or of.remote_reason() is None, of.remote_reason()); restore(s)
if sys.platform.startswith("linux") and not os.environ.get("WSL_DISTRO_NAME"):
    s = env(); check("Linux with no display → remote", "display" in (of.remote_reason() or "") or container, of.remote_reason()); restore(s)
s = env(WSL_DISTRO_NAME="Ubuntu"); check("WSL with no display → not remote (Windows' browser reaches 127.0.0.1)", container or of.remote_reason() is None, of.remote_reason()); restore(s)
s = env(SSH_TTY="/dev/pts/1", DRY_SIGNIN="browser"); check("DRY_SIGNIN=browser overrides the detection", of.remote_reason() is None); restore(s)
s = env(DISPLAY=":0", DRY_SIGNIN="paste"); check("DRY_SIGNIN=paste forces the paste sign-in", of.remote_reason() is not None); restore(s)

# ---- 2. reading the pasted address ----------------------------------------------------------------------------------
full = "http://127.0.0.1:41234/callback?code=AbC-123_x&state=st8&iss=https%3A%2F%2Fdry.ai"
check("the whole address", of.parse_pasted(full) == {"code": "AbC-123_x", "state": "st8", "iss": "https://dry.ai"})
check("quoted, with spaces", of.parse_pasted(f'  "{full}"  ')["code"] == "AbC-123_x")
check("just the ?query part", of.parse_pasted("?code=k1&state=s1") == {"code": "k1", "state": "s1"})
check("a refusal comes through as an error", of.parse_pasted("http://127.0.0.1:1/callback?error=access_denied&state=s")["error"] == "access_denied")
check("something else has no code", "code" not in of.parse_pasted("hello"))

# ---- 3. a terminal that cannot ask → the token route at once, nothing registered ------------------------------------
s = env(DRY_SIGNIN="paste")
try:
    of.sign_in(BASE, paste=None, say=lambda m: None); check("no terminal on a remote machine → stops at once with the token route", False)
except of.DryError as e:
    check("no terminal on a remote machine → stops at once with the token route", "hermes config set DRY_TOKEN" in str(e), str(e)[:90])
restore(s)

# ---- 4. the paste sign-in, end to end against Dry -------------------------------------------------------------------
said, asked = [], []
def say(m): said.append(m)
def paste_after_approving(prompt):
    asked.append(prompt)
    if len(asked) == 1: return "hello"                                    # a wrong paste first: asked again, not failed
    url = next(w for m in said for w in m.split() if "/oauth/authorize?" in w)
    return "  " + approve(url) + "  "                                     # the address the browser lands on, as copied
s = env(DRY_SIGNIN="paste")
try:
    r = of.sign_in(BASE, paste=paste_after_approving, say=say)
    me = of.who(r["base"], r["token"])
    check("the pasted address finishes the sign-in: a working token for the person who approved", me.get("userId") == USER, me.get("email") or me.get("userId"))
    check("the instructions say why and what to paste", any("Approve and connect" in m and "127.0.0.1" in m and "paste" in m for m in said))
    check("a wrong paste is asked again, not fatal", len(asked) == 2 and any("not the address" in m for m in said))
    check("its token is revoked again", revoke(r["token"]))
except Exception as e:
    check("the pasted address finishes the sign-in", False, f"{type(e).__name__}: {e}")
restore(s)

# ---- 5. remote, but the browser's answer reaches the listener anyway (an SSH tunnel): Enter finishes it -------------
said.clear(); asked.clear()
def tunnel_then_enter(prompt):
    asked.append(prompt)
    url = next(w for m in said for w in m.split() if "/oauth/authorize?" in w)
    urllib.request.urlopen(approve(url), timeout=20).read()               # the callback arrives on 127.0.0.1, as through ssh -L
    return ""
s = env(DRY_SIGNIN="paste")
try:
    r = of.sign_in(BASE, paste=tunnel_then_enter, say=say)
    check("a callback that reaches the listener finishes it on Enter", of.who(r["base"], r["token"]).get("userId") == USER)
    revoke(r["token"])
except Exception as e:
    check("a callback that reaches the listener finishes it on Enter", False, f"{type(e).__name__}: {e}")
restore(s)

print(f"\n{n - fails} passed · {fails} failed")
sys.exit(1 if fails else 0)
