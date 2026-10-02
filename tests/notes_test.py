"""Session notes on a model that thinks before answering (10/2, a remote Hermes): the old 400-token cap left such a model no room
to answer, so notes were never written and every session logged a warning. Run inside Hermes's runtime (no Dry needed):
  <hermes python> -I -c "import sys; sys.path.insert(0,'<hermes-agent>'); import runpy; runpy.run_path('tests/notes_test.py', run_name='__main__')"
"""
import importlib.util, logging, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dryplug", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
m = importlib.util.module_from_spec(spec); sys.modules["dryplug"] = m; spec.loader.exec_module(m)
n = fails = 0
def check(name, cond, detail=""):
    global n, fails
    n += 1; fails += 0 if cond else 1
    print(f"{'✓' if cond else '✗'} {n}. {name}" + (f" — {detail}" if detail else ""))
class R:
    def __init__(self, text): self.text = text
class Thinker:
    """Spends ~1,000 tokens thinking first: with less room it returns nothing; with room it answers inside <think>…</think> + the bullets."""
    def __init__(self): self.caps = []
    def complete(self, messages, max_tokens=None, **kw):
        self.caps.append(max_tokens)
        if max_tokens is not None and max_tokens < 1000: return R("")
        return R("<think>Let me weigh what lasts… " + "hmm " * 200 + "</think>\n- Prefers oat milk in coffee\n- Moving the launch to 10/14 because QA slipped")
warnings = []
class H(logging.Handler):
    def emit(self, rec): warnings.append(rec) if rec.levelno >= logging.WARNING and rec not in warnings else None   # one record, however many handlers see it
logging.getLogger().addHandler(H()); m.logger.addHandler(H())
t = Thinker(); m._llm = lambda: t
out = m.summarise_session([("user", "I take oat milk, never dairy."), ("assistant", "Noted."), ("user", "Launch moves to 10/14, QA slipped.")])
check("a thinking model has room to answer (cap ≥ 1000 tokens)", all(c is None or c >= 1000 for c in t.caps), str(t.caps))
check("its notes are written, the thinking stripped", out == "- Prefers oat milk in coffee\n- Moving the launch to 10/14 because QA slipped", repr(out[:120]))
check("no warning logged", not warnings, str([w.getMessage() for w in warnings]))
class Silent:
    def complete(self, *a, **k): return R("")
m._llm = lambda: Silent(); warnings.clear()
check("a model that answers nothing: no notes and no warning (an info line only)", m.summarise_session([("user", "hi")]) == "" and not warnings)
class Down:
    def complete(self, *a, **k): raise RuntimeError("provider down")
m._llm = lambda: Down(); warnings.clear()
check("a provider that fails twice still warns (a real fault)", m.summarise_session([("user", "hi")]) == "" and len(warnings) == 1)
print(f"\n{n - fails} passed · {fails} failed"); sys.exit(1 if fails else 0)
