"""Phase 3, live, through Hermes's real loaders: the agent's digest fed by the SSE stream (no polling for a live space), the
agent reacting on its own to other people's changes (batched, capped, never for the person's own edits), its cursor advanced
so its next turn does not repeat them, and the fall back to a plain alert (cap reached, or no gateway chat to react in).
A THROWAWAY HERMES_HOME only. Another person's edits need DRY_TEST_OTHER="<header>: <value>" (see live_test_phase2.py)."""
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

HOME = Path(os.environ["HERMES_HOME"])
assert HOME.resolve() != (Path.home() / ".hermes").resolve(), "refusing to run against the real ~/.hermes"
for line in (HOME / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
_other = os.environ.get("DRY_TEST_OTHER", "")
if not _other:
    print("○ phase 3 needs a second identity (DRY_TEST_OTHER) — skipped"); sys.exit(0)
OTHER_HEADER, _, OTHER = (x.strip() for x in _other.partition(":"))

n = fails = 0
def check(name, cond, detail=""):
    global n, fails
    n += 1; fails += 0 if cond else 1
    print(f"{'✓' if cond else '✗'} {n}. {name}" + (f" — {detail}" if detail else ""))
def wait(pred, secs=20.0):
    t = time.time()
    while time.time() - t < secs:
        if pred(): return True
        time.sleep(0.3)
    return False

stamp = str(int(time.time()))
for f in ("dry-cursors.json", "dry-own-writes.json", "dry-recent-changes.json", "dry.json", "dry-react-session.json"):
    (HOME / f).unlink(missing_ok=True)
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load(force=True)
from plugins.memory import load_memory_provider
from agent.memory_manager import MemoryManager
prov = load_memory_provider("dry")
mod = sys.modules[type(prov).__module__]
aw = sys.modules[mod.__name__ + ".awareness"]
mgr = MemoryManager(); mgr.add_provider(prov)
mgr.initialize_all(session_id=f"p3-{stamp}", platform="telegram", hermes_home=str(HOME), agent_context="primary", gateway_session_key=f"agent:main:telegram:dm:{stamp}")
st = prov._st(str(HOME)); client, _, _ = prov._ensure_space(st); me = st["me"]
chat = json.loads((HOME / "dry-react-session.json").read_text())
check("a messaging chat is remembered for reactions (gateway_session_key)", chat.get("session_key") == f"agent:main:telegram:dm:{stamp}" and chat.get("platform") == "telegram")

def as_other(method, path, body=None):
    req = urllib.request.Request(f"{os.environ['DRY_URL']}/api{path}", method=method, headers={OTHER_HEADER: OTHER, "content-type": "application/json"}, data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req) as r: return json.loads(r.read() or b"null")

sp = client.request("POST", "/spaces", {"name": f"Hermes react test {stamp}", "public": False})
task = client.request("POST", f"/spaces/{sp['id']}/types", {"name": "Task", "fields": [{"label": "Title", "kind": "text", "mode": "required"}]})
client.request("POST", f"/spaces/{sp['id']}/members", {"userId": OTHER, "roles": ["member"]})
mod.save_settings({"watch": [sp["id"]], "react": True, "react_per_hour": 2, "notify_to": ""}, str(HOME))

# the agent looks once (cursor set), then a watcher in THIS process streams the space, as in the gateway
st["last_poll"] = 0; prov.prefetch(f"hello {stamp}")
reactions, sent = [], []
w = aw.Watcher(client, str(HOME), me, lambda: mod.load_settings(str(HOME)), lambda t, m: sent.append((t, m)), lambda f, name: threading.Thread(target=f, name=name, daemon=True), flush_s=1.5)
w.react = lambda items: (reactions.append(items) or True)
aw._watchers[str(HOME)] = w
w.start()
check("the stream connects", wait(lambda: w.live.get(sp["id"]), 15))

# 1. another person adds a task → the agent reacts (no alert target needed), within seconds
t0 = time.time()
as_other("POST", f"/spaces/{sp['id']}/objects", {"typeId": task["id"], "values": {"Title": f"Fix the login bug {stamp}"}})
check("another person's change → the agent reacts within seconds", wait(lambda: reactions), f"{time.time() - t0:.1f}s")
check("…the reaction carries the change in plain words with its link", reactions and f"Fix the login bug {stamp}" in aw.sentence(reactions[0][0]) and "/o/" in aw.sentence(reactions[0][0]), aw.sentence(reactions[0][0]) if reactions else "")
check("…no plain alert is sent when the agent reacted", not sent)
st["last_poll"] = 0
nxt = prov.prefetch(f"anything new? {stamp}")
check("…the agent's next turn does NOT repeat what it reacted to", f"Fix the login bug {stamp}" not in nxt, nxt[:80] or "(nothing)")

# 2. the person's OWN edit never triggers a reaction — and still reaches the agent's next turn
n_react = len(reactions)
client.create_object(sp["id"], task["id"], {"Title": f"My own task {stamp}"})
time.sleep(4)
check("the person's own edit does not start a reaction", len(reactions) == n_react)
calls = {"n": 0}
real_changes = client.changes
def counting(*a, **k):
    calls["n"] += 1; return real_changes(*a, **k)
client.changes = counting
st["last_poll"] = 0
own = prov.prefetch(f"and now? {stamp}")
client.changes = real_changes
check("…it reaches the agent's next turn as 'you added'", f"you added Task “My own task {stamp}”" in own, own.splitlines()[1] if own.count("\n") else own)
check("…read from the live stream: no change-feed request for the streamed space", calls["n"] == 0, f"{calls['n']} request(s)")

# 3. the cap: react_per_hour=2 → the next batch is a plain alert
as_other("POST", f"/spaces/{sp['id']}/objects", {"typeId": task["id"], "values": {"Title": f"Second report {stamp}"}})
wait(lambda: len(reactions) >= n_react + 1, 15)
mod.save_settings({"notify_to": "telegram"}, str(HOME))
as_other("POST", f"/spaces/{sp['id']}/objects", {"typeId": task["id"], "values": {"Title": f"Third report {stamp}"}})
check("past the hourly cap → a plain alert instead of a reaction", wait(lambda: any(f"Third report {stamp}" in m for _, m in sent), 15) and not any(f"Third report {stamp}" in aw.sentence(i) for r in reactions for i in r), f"{len(reactions)} reactions · {len(sent)} alert(s)")
w.stop.set(); aw._watchers.pop(str(HOME), None)

# 4. Hermes's real inject_message outside a gateway: refused → the plugin falls back to the alert path
ok = mod._react(str(HOME), [{"seq": 1, "spaceId": sp["id"], "space": "x", "kind": "object", "op": "create", "actor": "someone", "type": "Task", "title": "t", "url": ""}])
check("outside a gateway the real inject_message refuses (no live session) → False, so the alert path is used", ok is False)

# 5. /dry watch react
check("/dry watch react off", "no longer react" in mod.dry_command("watch react off") and mod.load_settings(str(HOME))["react"] is False)
check("/dry watch react on", "will react on its own" in mod.dry_command("watch react on") and mod.load_settings(str(HOME))["react"] is True)
check("/dry watch status names the chat reactions go to", "into your telegram chat" in mod.dry_command("watch status"), mod.dry_command("watch status")[:140])

client.request("DELETE", f"/spaces/{sp['id']}", query={"force": "true"})
check("cleanup: the test space is deleted", not any(s["id"] == sp["id"] for s in client.spaces()))
mod.save_settings({"notify_to": "", "watch": "all", "react": True, "react_per_hour": 6}, str(HOME))
print(f"{'ALL ' + str(n) + ' PASSED' if not fails else f'FAILED {fails} of {n}'}")
sys.exit(1 if fails else 0)
