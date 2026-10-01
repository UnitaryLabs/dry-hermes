"""Live test of the Dry plugin through Hermes's REAL loaders, against a running Dry.

Run inside Hermes's runtime with a THROWAWAY home (never your real ~/.hermes) whose config enables the plugin:
  HERMES_HOME=<tmp home> <hermes python> -I -c "import sys; sys.path.insert(0,'<hermes-agent>'); import hermes_bootstrap; import runpy; runpy.run_path('tests/live_test.py', run_name='__main__')"
The home needs: plugins/dry → this repo · config.yaml {plugins.enabled: [dry], memory.provider: dry} · .env with DRY_TOKEN (+ DRY_URL).
Creates a few Memory records in the token owner's Memories space and deletes every one it made.
"""
import json
import os
import sys
import time
from pathlib import Path

HOME = Path(os.environ["HERMES_HOME"])
assert HOME.resolve() != (Path.home() / ".hermes").resolve(), "refusing to run against the real ~/.hermes"
for line in (HOME / ".env").read_text().splitlines():            # what the CLI does at start-up
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

n = fails = 0
def check(name, cond, detail=""):
    global n, fails
    n += 1
    fails += 0 if cond else 1
    print(f"{'✓' if cond else '✗'} {n}. {name}" + (f" — {detail}" if detail else ""))

stamp = str(int(time.time()))
made = []

# 1. the GENERAL loader: the plugin loads as `kind: standalone`, registers its command and skills
from hermes_cli.plugins import get_plugin_manager
pm = get_plugin_manager()
pm.discover_and_load(force=True)
plugins = getattr(pm, "_plugins", {}) or {}
mine = next((p for p in plugins.values() if p.manifest.name == "dry"), None)
check("general loader: plugin 'dry' loaded, enabled, no error", mine is not None and mine.enabled and not mine.error, (mine.error if mine else "not discovered") or "")
cmds = getattr(pm, "_plugin_commands", None) or getattr(pm, "_commands", {}) or {}
check("slash command /dry registered", "dry" in cmds, ",".join(list(cmds)[:10]))
sk = [pm.find_plugin_skill("dry:using-dry"), pm.find_plugin_skill("dry:memories")]
check("skills dry:using-dry and dry:memories registered", all(sk) and all(Path(x).exists() for x in sk), str([Path(x).parent.name for x in sk if x]))

# 2. the MEMORY loader: provider by name, available with the token, initialised like an agent does
from plugins.memory import load_memory_provider
from agent.memory_manager import MemoryManager
prov = load_memory_provider("dry")
check("memory loader: provider 'dry' loads", prov is not None and prov.name == "dry")
check("is_available() with DRY_TOKEN set (no network)", prov.is_available())
mgr = MemoryManager()
mgr.add_provider(prov)
mgr.initialize_all(session_id=f"t-{stamp}", platform="cli", hermes_home=str(HOME), agent_context="primary")
for _ in range(50):
    st = prov._st(str(HOME))
    if st.get("space") or st.get("error"):
        break
    time.sleep(0.2)
st = prov._st(str(HOME))
check("initialize warms the Memories space in the background", bool(st.get("space")), st.get("error") or (st.get("space") or {}).get("name", ""))
schemas = [s["name"] for s in prov.get_tool_schemas()]
check("tools: dry_remember · dry_recall · dry_changes · dry_forget", schemas == ["dry_remember", "dry_recall", "dry_changes", "dry_forget"], str(schemas))
block = prov.system_prompt_block()
check("system prompt names the space with its link and the skills", "/s/" in block and "dry:using-dry" in block, block.splitlines()[1][:120])

# 3. tools through the MemoryManager routing
r = json.loads(mgr.handle_tool_call("dry_remember", {"title": f"Prefers oat milk {stamp}", "note": "Asked twice for oat milk in coffee; never dairy.", "source": "https://example.com/coffee"}))
check("dry_remember saves a Memory and returns its link", r.get("ok") and "/o/" in r.get("url", ""), r.get("url") or r.get("error"))
if r.get("ok"): made.append(r["id"])
client, space, mtype = prov._ensure_space(st)
o = client.request("GET", f"/spaces/{space['id']}/objects/{r.get('id')}") if r.get("ok") else {}
field_value = sys.modules[prov.__class__.__module__].field_value
check("…the record holds Title, Note, Source and today's When", field_value(o, mtype, "Title") == f"Prefers oat milk {stamp}" and "oat milk" in (field_value(o, mtype, "Note") or "") and field_value(o, mtype, "Source") == "https://example.com/coffee" and bool(field_value(o, mtype, "When")))
time.sleep(1.0)  # search text / embeddings are written with the record; give a semantic index a moment
rec = json.loads(mgr.handle_tool_call("dry_recall", {"query": f"oat milk {stamp}"}))
check("dry_recall finds it, with its link", rec.get("ok") and any(x["id"] == r.get("id") for x in rec.get("results", [])), str([x.get("title") for x in rec.get("results", [])])[:160])
pre = prov.prefetch(f"What milk do I take in my coffee? oat milk {stamp}")
mem_part = pre[pre.find("## From your Dry memories"):] if "## From your Dry memories" in pre else ""
check("prefetch adds it before the turn under 'From your Dry memories'", f"Prefers oat milk {stamp}" in mem_part, mem_part.splitlines()[1][:140] if mem_part.count("\n") else "(empty)")
check("prefetch skips a trivial prompt (no network call)", prov.prefetch("ok") == "")
alls = json.loads(mgr.handle_tool_call("dry_recall", {"query": f"oat milk {stamp}", "all_spaces": True}))
check("dry_recall all_spaces searches every space", alls.get("ok") and any(x["id"] == r.get("id") for x in alls.get("results", [])), str(len(alls.get("results", []))))

# 4. mirroring Hermes's own memory writes (add → replace → remove)
def mirrored(text):
    items = client.search(space["id"], text, type_id=mtype["id"], limit=10)
    return [x for x in items if text in (field_value(x, mtype, "Note") or "")]
a = f"Works on the garden project with a neighbour {stamp}"
prov.on_memory_write("add", "memory", a); prov._writer.join(10)
got = mirrored(a)
check("built-in memory ADD is mirrored as a Memory record", len(got) == 1, str(len(got)))
b = f"Works on the garden project with two neighbours {stamp}"
prov.on_memory_write("replace", "memory", b, {"previous_content": a}); prov._writer.join(10)
got_b = mirrored(b)
check("REPLACE updates the same record (no duplicate)", len(got_b) == 1 and got and got_b[0]["id"] == got[0]["id"] and not mirrored(a), f"{len(got_b)} · same id {bool(got and got_b and got_b[0]['id']==got[0]['id'])}")
u = f"Lives in California, Pacific time {stamp}"
prov.on_memory_write("add", "user", u); prov._writer.join(10)
gu = mirrored(u)
check("a USER.md fact is titled 'About the person: …'", len(gu) == 1 and (field_value(gu[0], mtype, "Title") or "").startswith("About the person:"), (field_value(gu[0], mtype, "Title") if gu else ""))
prov.on_memory_write("remove", "memory", b, {"previous_content": b}); prov._writer.join(10)
check("REMOVE deletes the mirrored record", not mirrored(b))
prov.on_memory_write("remove", "user", u, {"previous_content": u}); prov._writer.join(10)
sub = MemoryManager(); sp = load_memory_provider("dry"); sub.add_provider(sp); sub.initialize_all(session_id=f"s-{stamp}", platform="cli", hermes_home=str(HOME), agent_context="subagent")
sp.on_memory_write("add", "memory", f"subagent note {stamp}"); time.sleep(1.0)
check("a subagent's memory write is NOT mirrored", not mirrored(f"subagent note {stamp}"))

# 5. the /dry command
from importlib import import_module
handler = (cmds.get("dry") or {}).get("handler") if isinstance(cmds.get("dry"), dict) else None
if handler is None:
    handler = sys.modules[prov.__class__.__module__].dry_command
out = handler(f"find oat milk {stamp}")
check("/dry find returns the memory with its link", f"Prefers oat milk {stamp}" in out and "/o/" in out, out.splitlines()[0][:140])
out = handler("status")
check("/dry status: account, address, memory count", out.startswith("Dry: signed in as") and "memories:" in out, out[:160])
out = handler(f"save Parking spot is B12 {stamp}")
check("/dry save stores a memory", out.startswith("Saved to Dry:"), out[:120])
if "/o/" in out: made.append(out.rsplit("/o/", 1)[1].strip())

# 6. forget (and the guard: only Memories-space records)
r2 = json.loads(mgr.handle_tool_call("dry_forget", {"memory": r.get("url", "")}))
check("dry_forget deletes by link", r2.get("ok"), str(r2)[:120])
check("…and it is gone", not any(x["id"] == r.get("id") for x in json.loads(mgr.handle_tool_call("dry_recall", {"query": f"oat milk {stamp}"})).get("results", [])))
outside = None
for s_ in client.spaces():
    if s_["id"] != space["id"]:
        some = client.find_objects(s_["id"], limit=1)
        if some:
            outside = some[0]; break
g = json.loads(mgr.handle_tool_call("dry_forget", {"memory": outside["id"]})) if outside else {}
check("dry_forget refuses a record outside Memories (and leaves it)", outside is not None and not g.get("ok") and "not in the Memories space" in g.get("error", "") and client.locate(outside["id"]).get("id") == outside["id"], str(g)[:120])

# 7. a bad token never raises into the agent
os.environ["DRY_TOKEN"] = "dry_pat_" + "x" * 32   # deliberately invalid
bad = load_memory_provider("dry"); bad._state.clear()
res = json.loads(bad.handle_tool_call("dry_recall", {"query": "x"}))
check("a revoked/bad token → a JSON error, never an exception", res.get("ok") is False and "401" in res.get("error", ""), res.get("error", "")[:120])
check("…and prefetch stays silent", bad.prefetch("what do I know about coffee?") == "")

# cleanup: every record this run made
for ref in made[1:]:
    try: client.delete_object(space["id"], client.locate(ref)["id"])   # a link's short id → the record's UUID
    except Exception as e: print("cleanup:", e)
left = [x for x in client.find_objects(space["id"], type_id=mtype["id"], search=stamp, mode="text", limit=50)]
check("cleanup: nothing this run made is left in Memories", not left, str(len(left)))
mgr.shutdown_all()
print(f"{'ALL ' + str(n) + ' PASSED' if not fails else f'FAILED {fails} of {n}'}")
sys.exit(1 if fails else 0)
