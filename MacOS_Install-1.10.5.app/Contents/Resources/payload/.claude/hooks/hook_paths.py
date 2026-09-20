"""Installation resources are independent of the project that triggered a hook."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "hooks-runtime"
GLOBAL_INSTALL = ROOT == (Path.home() / ".claude").resolve()


def bypass_dir(project=None):
    # Keep compatibility with markers from existing project installations.
    if GLOBAL_INSTALL:
        return RUNTIME / "bypass"
    return Path(project) / ".claude/hooks-runtime" if project else RUNTIME
