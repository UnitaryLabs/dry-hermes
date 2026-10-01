"""`hermes dry …` — shown while dry is the active memory provider.

Hermes loads this file on its own (not as part of the plugin package), so it reaches the plugin through the memory loader."""
import sys


def _plugin():
    from plugins.memory import load_memory_provider
    provider = load_memory_provider("dry")
    if provider is None:
        raise SystemExit("The dry memory provider is not installed or not available (DRY_TOKEN missing?).")
    return sys.modules[type(provider).__module__]


def _run(args) -> None:
    mod = _plugin()
    sub = getattr(args, "dry_command", None) or "status"
    if sub == "status":
        print(mod.status_summary())
    else:
        print(mod.dry_command(f"{sub} {' '.join(getattr(args, 'text', []) or [])}".strip()))


def register_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="dry_command")
    subs.add_parser("status", help="Account, address and memory count")
    subs.add_parser("spaces", help="Your Dry spaces, with links")
    for name, help_ in (("find", "Search your Dry memories"), ("save", "Save a memory")):
        p = subs.add_parser(name, help=help_)
        p.add_argument("text", nargs="*")
    subparser.set_defaults(func=_run)
