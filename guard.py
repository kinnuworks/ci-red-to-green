"""Which changed files may go into the fix PR.

The plugin builds the PR commit itself from the worktree, and only from files
this module calls source. Test files, test config and CI config never go in,
so GitHub runs the original tests against the fix.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import PurePosixPath

_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".tox", "build", "dist"}
_TEST_CONFIG = {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "noxfile.py"}
# Files that would be imported in place of the test runner or run at interpreter start.
_SHADOWS = {"pytest.py", "_pytest.py", "sitecustomize.py", "usercustomize.py", "pytest", "_pytest"}


def is_protected(path: str) -> bool:
    """True for files a fix must not change: tests, test config, CI config."""
    p = PurePosixPath(path)
    name = p.name
    parts = set(p.parts[:-1])
    if parts & {"tests", "test", "testing"}:
        return True
    if name.startswith("test_") or name.endswith(("_test.py", "_test.go")) or name in ("tests.py", "test.py"):
        return True
    if name in _TEST_CONFIG or name in _SHADOWS or (p.parts and p.parts[0] in _SHADOWS) or name.endswith(".pth"):
        return True
    if p.parts and p.parts[0] == ".github":
        return True
    if parts & {"snapshots", "__snapshots__", "fixtures", "testdata"}:
        return True
    return False


def snapshot(root: str) -> dict[str, str]:
    """sha256 of every regular file under root, keyed by posix relative path."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        # git submodules and nested repos are other projects: never part of the fix, so not scanned
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not os.path.exists(os.path.join(dirpath, d, ".git"))]
        for f in filenames:
            full = os.path.join(dirpath, f)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if f.endswith((".pyc", ".pyo")):
                continue
            h = hashlib.sha256()
            with open(full, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            out[rel] = h.hexdigest()
    return out


def changes(before: dict[str, str], after: dict[str, str]) -> dict[str, str]:
    """path -> 'added' | 'modified' | 'deleted'."""
    ch: dict[str, str] = {}
    for p, h in after.items():
        if p not in before:
            ch[p] = "added"
        elif before[p] != h:
            ch[p] = "modified"
    for p in before:
        if p not in after:
            ch[p] = "deleted"
    return ch


def split(ch: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """(source changes that may ship, protected changes that block the fix)."""
    src = {p: k for p, k in ch.items() if not is_protected(p)}
    prot = {p: k for p, k in ch.items() if is_protected(p)}
    return src, prot
