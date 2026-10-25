"""Real temporary engineering history, separate from artificial knowledge Git."""

from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from git_fixtures import configure_test_repository
from test_pipeline import git
from pdm.skill_support import PROJECT_ROOT


def isolated_engine(case):
    temporary = tempfile.TemporaryDirectory(prefix="pdm-engine-fixture-")
    case.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    for name in ("src/pdm", "prompts", "skills"):
        shutil.copytree(PROJECT_ROOT / name, root / "project" / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(PROJECT_ROOT.parent / "pyproject.toml", root / "pyproject.toml")
    git(root, "init", "-b", "dev")
    configure_test_repository(root)
    git(root, "config", "user.name", "Artificial Public Engine")
    git(root, "config", "user.email", "engine@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-m", "artificial initial public engine")
    for module in ("engine_provenance", "pipeline"):
        fixture = patch("pdm." + module + ".__file__", str(root / "project/src/pdm" / (module + ".py")))
        fixture.start()
        case.addCleanup(fixture.stop)
    return root


def advance_engine(root):
    path = root / "project/src/pdm/fixture_upgrade.py"
    path.write_text("# Artificial engineering upgrade; no runtime behavior change.\n")
    git(root, "add", str(path.relative_to(root)))
    git(root, "commit", "-m", "artificial public engine upgrade")
