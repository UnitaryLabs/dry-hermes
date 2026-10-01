"""Phase 2, live, through Hermes's real loaders: change awareness (polled per turn), the agent's own writes filtered out,
dry_changes, the SSE watcher (live push + replay after a reconnect), session notes on the real model, the person's
profile in the system prompt, /dry watch. Same rules as live_test.py: a THROWAWAY HERMES_HOME, never ~/.hermes.
Another person's edits need a second identity on a Dry where one can be impersonated for testing: set DRY_TEST_OTHER as
"<header name>: <value>" (the header that makes a request act as a second member); without it those checks are skipped.
Everything it creates lives in one test space (deleted at the end) plus a few Memory records (deleted)."""
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
LOCAL = bool(_other)                                  # a second identity is available
OTHER_HEADER, _, OTHER_VALUE = (x.strip() for x in _other.partition(":"))
OTHER = OTHER_VALUE                                   # the second member's user id

n = fails = 0
def check(name, cond, detail=""):
    global n, fails
    n += 1; fails += 0 if cond else 1
    print(f"{'✓' if cond else '✗'} {n}. {name}" + (f" — {detail}" if detail else ""))
def skip(name, why):
    print(f"○ {name} — {why}")

stamp = str(int(time.time()))
for f in ("dry-cursors.json", "dry-own-writes.json", "dry-recent-changes.json", "dry-memory-map.json", "dry.json"):
    (HOME / f).unlink(missing_ok=True)

from hermes_cli.plugins import get_plugin_manager
pm = get_plugin_manager(); pm.discover_and_load(force=True)
from plugins.memory import load_memory_provider
from agent.memory_manager import MemoryManager
prov = load_memory_provider("dry")
mod = sys.modules[type(prov).__module__]
aw = sys.modules[mod.__name__ + ".awareness"]
mgr = MemoryManager(); mgr.add_provider(prov)
mgr.initialize_all(session_id=f"p2-{stamp}", platform="cli", hermes_home=str(HOME), agent_context="primary", session_title="Phase 2 test")
st = prov._st(str(HOME))
client, mem_space, mtype = prov._ensure_space(st)
me = st["me"]

def as_other(method, path, body=None):
    req = urllib.request.Request(f"{os.environ['DRY_URL']}/api{path}", method=method, headers={OTHER_HEADER: OTHER_VALUE, "content-type": "application/json"}, data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req) as r: return json.loads(r.read() or b"null")

# a test space the person owns, with a Task type, the other person a member (local)
sp = client.request("POST", "/spaces", {"name": f"Hermes watch test {stamp}", "public": False})
task = client.request("POST", f"/spaces/{sp['id']}/types", {"name": "Task", "fields": [{"label": "Title", "kind": "text", "mode": "required"}, {"label": "Status", "kind": "text"}]})
if LOCAL:
    client.request("POST", f"/spaces/{sp['id']}/members", {"userId": OTHER, "roles": ["member"]})
mod.save_settings({"watch": [sp["id"]]}, str(HOME))

# 1. the first look sets a cursor and reports nothing old
st["last_poll"] = 0
first = prov.prefetch("hello there, what's new?")
check("first look: cursor set at the end of the feed, no flood of history", "Hermes watch test" not in first, first[:80] or "(nothing)")
cur = json.loads((HOME / "dry-cursors.json").read_text())["agent"].get(sp["id"])
check("…the agent cursor is stored per profile", isinstance(cur, int), str(cur))

# 2. the person edits in the web app (same account, not through the agent) · another person adds a task · the agent writes one itself
mine = client.create_object(sp["id"], task["id"], {"Title": f"Book the venue {stamp}", "Status": "open"})
if LOCAL:
    theirs = as_other("POST", f"/spaces/{sp['id']}/objects", {"typeId": task["id"], "values": {"Title": f"Order the cake {stamp}"}})
agent_made = client.create_object(sp["id"], task["id"], {"Title": f"Agent wrote this {stamp}"})
mod._on_post_tool_call("mcp__dry__create_object", {"spaceId": sp["id"]}, json.dumps({"id": agent_made["id"], "url": "x"}))
st["last_poll"] = 0
block = prov.prefetch(f"anything I should know about the party planning {stamp}?")
check("next turn: the person's own edit is reported as 'you added'", f"you added Task “Book the venue {stamp}”" in block, block.splitlines()[0] if block else "(empty)")
if LOCAL:
    check("…another person's edit is reported with their name or 'someone'", f"Order the cake {stamp}" in block and "you added Task “Order the cake" not in block)
else:
    skip("another person's edit", "needs a second identity (DRY_TEST_OTHER)")
check("…the agent's own write (through Dry's MCP tools) is NOT reported", f"Agent wrote this {stamp}" not in block)
check("…each line carries the record's link", "/o/" in block)
check("header: 'Changed in Dry just now' once the session has started", block.startswith("## Changed in Dry just now"), block.splitlines()[0] if block else "")
st["last_poll"] = 0
check("nothing new → no block (the cursor advanced)", "Book the venue" not in prov.prefetch(f"and now? {stamp}"))

# 3. dry_changes on demand
r = json.loads(mgr.handle_tool_call("dry_changes", {"hours": 1}))
check("dry_changes lists recent changes in plain words", r.get("ok") and any(f"Book the venue {stamp}" in c["what"] for c in r["changes"]), str(len(r.get("changes", []))))
if LOCAL:
    r2 = json.loads(mgr.handle_tool_call("dry_changes", {"hours": 1, "others_only": True}))
    check("dry_changes others_only drops the person's own edits", any("Order the cake" in c["what"] for c in r2["changes"]) and not any("Book the venue" in c["what"] for c in r2["changes"]))
mod.save_settings({"watch": "all"}, str(HOME))

# 4. SSE watcher: live push of another person's edit, batched, sent to the target; own edits not alerted; resume after reconnect
sent = []
mod.save_settings({"notify_to": "telegram", "watch": [sp["id"]]}, str(HOME))
w = aw.Watcher(client, str(HOME), me, lambda: mod.load_settings(str(HOME)), lambda t, m: sent.append((t, m)), lambda f, name: threading.Thread(target=f, name=name, daemon=True), flush_s=2.0)
w.start()
time.sleep(3.0)                                        # streams connected
t0 = time.time()
if LOCAL:
    live = as_other("POST", f"/spaces/{sp['id']}/objects", {"typeId": task["id"], "values": {"Title": f"Live from the other person {stamp}"}})
else:
    live = client.create_object(sp["id"], task["id"], {"Title": f"Live from the other person {stamp}"})
    mod.save_settings({"notify_own": True}, str(HOME))
for _ in range(30):
    if any(f"Live from the other person {stamp}" in m for _, m in sent):
        break
    time.sleep(0.5)
lat = time.time() - t0
check("SSE: a change pushed by Dry reaches the target within seconds", any(f"Live from the other person {stamp}" in m for _, m in sent), f"{lat:.1f}s · {len(sent)} message(s)")
check("…sent to the configured target, as plain text with a link", sent and sent[-1][0] == "telegram" and sent[-1][1].startswith("🔔 New in Dry") and "/o/" in sent[-1][1], (sent[-1][1].splitlines()[0] if sent else ""))
if LOCAL:
    n_before = len(sent)
    client.create_object(sp["id"], task["id"], {"Title": f"My own edit {stamp}"})
    time.sleep(5)
    check("…the person's own edit is NOT alerted (notify_own off)", len(sent) == n_before or not any(f"My own edit {stamp}" in m for _, m in sent[n_before:]))
w.stop.set()
# resume: from a cursor before the live record, the stream replays it (Last-Event-ID)
replayed = []
before = (aw._read(str(HOME), "dry-cursors.json", {}).get("watch", {}).get(sp["id"]) or 1) - 3
def _one():
    try:
        for ev in client.stream_events(sp["id"], max(before, 0)):
            replayed.append(ev)
            if len(replayed) >= 1: return
    except Exception as e:
        replayed.append({"error": str(e)})
th = threading.Thread(target=_one, daemon=True); th.start(); th.join(10)
check("SSE resume: reconnecting with Last-Event-ID replays what was missed", any(e.get("spaceId") == sp["id"] and e.get("seq", 0) > before for e in replayed), str(replayed[:1])[:120])
mod.save_settings({"notify_to": "", "notify_own": False, "watch": "all"}, str(HOME))

# 5. /dry watch (outside the gateway: saved, told to restart the gateway)
check("/dry watch status", mod.dry_command("watch").startswith("Live Dry alerts: OFF"), mod.dry_command("watch"))
out = mod.dry_command("watch on telegram")
check("/dry watch on <target> saves it and explains the gateway", "on → telegram" in out and "gateway" in out, out[:120])
check("/dry watch off", mod.dry_command("watch off") == "Live Dry alerts are off." and mod.load_settings(str(HOME))["notify_to"] == "")
check("/dry changes", "Changes in Dry" in mod.dry_command("changes 1") or "No changes" in mod.dry_command("changes 1"))

# 6. the person's profile (About the person: …) in the system prompt — edited in Dry, the agent knows it next session
fact = f"About the person: Allergic to peanuts {stamp}"
prof = client.create_object(mem_space["id"], mtype["id"], {"Title": fact, "Note": f"Allergic to peanuts {stamp}"})
mgr2 = MemoryManager(); p2 = load_memory_provider("dry"); mgr2.add_provider(p2)
mgr2.initialize_all(session_id=f"p2b-{stamp}", platform="cli", hermes_home=str(HOME), agent_context="primary")
sp_block = p2.system_prompt_block()
check("profile: 'About the person' memories appear in the system prompt", f"Allergic to peanuts {stamp}" in sp_block and "What you know about the person" in sp_block)

# 7. session notes on the person's own model (ctx.llm), one memory per session, upserted
talk = [{"role": "user", "content": "We're planning Mia's 7th birthday party."}, {"role": "assistant", "content": "Great — when and where?"},
        {"role": "user", "content": f"Saturday 18 October at Lakeside Park, about 15 kids. Budget 300 dollars. ({stamp})"}, {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "Mia wants a dinosaur theme, and remember: no peanuts anywhere, one child is allergic."}, {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "I decided to order the cake from Rosie's Bakery, not the supermarket."}, {"role": "assistant", "content": "Good choice."}]
if mod._llm() is None:
    skip("session notes", "no model facade in this Hermes")
else:
    prov.on_session_end(talk)
    notes = [o for o in client.find_objects(mem_space["id"], type_id=mtype["id"], search="Session notes", mode="text", limit=20)
             if str(mod.field_value(o, mtype, "Title") or "").startswith("Session notes") and "Phase 2 test" in str(mod.field_value(o, mtype, "Title"))]
    body = mod.field_value(notes[0], mtype, "Note") if notes else ""
    check("session notes: one Memory 'Session notes — <date> · <title>'", len(notes) == 1, mod.field_value(notes[0], mtype, "Title") if notes else "none")
    ok = body and body.startswith("- ") and ("18 October" in body or "Oct" in body) and ("Rosie" in body) and ("peanut" in body.lower())
    check("…bullets with the lasting points (date, bakery, allergy) — substance, not wording", bool(ok), (body or "")[:200].replace("\n", " | "))
    prov.on_pre_compress(talk); time.sleep(3)
    notes2 = [o for o in client.find_objects(mem_space["id"], type_id=mtype["id"], search="Session notes", mode="text", limit=20) if "Phase 2 test" in str(mod.field_value(o, mtype, "Title"))]
    check("…before compression the same session's memory is UPDATED, not duplicated", len(notes2) == 1)
    short = MemoryManager(); p3 = load_memory_provider("dry"); short.add_provider(p3)
    short.initialize_all(session_id=f"short-{stamp}", platform="cli", hermes_home=str(HOME), agent_context="primary", session_title=f"short {stamp}")
    p3.on_session_end(talk[:2]); time.sleep(1)
    check("…a short chat (< 4 messages from the person) writes no notes", not [o for o in client.find_objects(mem_space["id"], type_id=mtype["id"], search=f"short {stamp}", mode="text", limit=5)])
    for o in notes2:
        client.delete_object(mem_space["id"], o["id"])

# cleanup
client.delete_object(mem_space["id"], prof["id"])
try:
    client.request("DELETE", f"/spaces/{sp['id']}", query={"force": "true"})   # the owner deletes the space with everything in it
    gone = not any(s["id"] == sp["id"] for s in client.spaces())
except Exception as e:
    gone = False; print("cleanup:", e)
check("cleanup: the test space is deleted", gone)
mgr.shutdown_all()
print(f"{'ALL ' + str(n) + ' PASSED' if not fails else f'FAILED {fails} of {n}'}")
sys.exit(1 if fails else 0)
