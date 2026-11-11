"""`/ci demo`: the whole loop on a bundled sample, with no GitHub token and no network.

The sample (demo/humanize_mini) is trimmed from humanize and carries the real bug humanize
fixed in PR #329: naturalsize(999999) gives "1000.0 kB" instead of "1.0 MB". The red CI log
(demo/ci-log.txt) is pytest's real output on that sample. DemoGitHub stands in for the GitHub
client: it serves that one red run, and "opening a PR" writes the commit as a patch file.
"""

from __future__ import annotations

import difflib
import hashlib
import shutil
from pathlib import Path

SAMPLE = Path(__file__).parent / "demo" / "humanize_mini"
LOG = Path(__file__).parent / "demo" / "ci-log.txt"
REPO = "demo/humanize_mini"
RUN_ID = 329


def _files() -> dict[str, bytes]:
    out = {}
    for p in sorted(SAMPLE.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"):
            out[p.relative_to(SAMPLE).as_posix()] = p.read_bytes()
    return out


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


HEAD_SHA = hashlib.sha1(b"".join(_blob_sha(d).encode() for d in _files().values())).hexdigest()


def copy_sample(dest: str) -> None:
    """A fresh copy of the sample, so every demo starts from the red commit."""
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(SAMPLE, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))


class DemoGitHub:
    """The parts of the GitHub client the engine uses, answered from the bundled sample."""

    def __init__(self, token: str | None, repo: str, out_dir: Path):
        self.repo = repo
        self.out_dir = Path(out_dir)
        self._files = _files()
        self._by_sha = {_blob_sha(d): d for d in self._files.values()}
        self.patch_path = self.out_dir / "demo-fix.patch"

    def runs(self, branch: str | None = None, head_sha: str | None = None, per_page: int = 10) -> list[dict]:
        if head_sha:
            return []  # nothing runs CI on a demo "PR"
        return [{"id": RUN_ID, "run_number": RUN_ID, "status": "completed", "conclusion": "failure",
                 "head_sha": HEAD_SHA, "html_url": "", "head_branch": "main"}]

    def failed_jobs(self, run_id: int) -> list[dict]:
        return [{"id": 1, "name": "test"}]

    def job_log(self, job_id: int) -> str:
        return LOG.read_text()

    def tree(self, sha: str) -> dict[str, str]:
        return {path: _blob_sha(d) for path, d in self._files.items()}

    def blob(self, sha: str) -> bytes:
        return self._by_sha.get(sha, b"")

    def compare(self, base: str, head: str) -> list[dict]:
        return []

    def create_branch_commit(self, base_sha: str, files: dict[str, bytes | None], message: str, branch: str) -> str:
        chunks = [f"Subject: {message}\n\n"]
        for path, new in sorted(files.items()):
            old = self._files.get(path, b"").decode("utf-8", "replace").splitlines(keepends=True)
            now = (new or b"").decode("utf-8", "replace").splitlines(keepends=True)
            chunks.extend(difflib.unified_diff(old, now, f"a/{path}", f"b/{path}"))
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.patch_path.write_text("".join(chunks))
        return _blob_sha(self.patch_path.read_bytes())

    def open_pr(self, head: str, base: str, title: str, body: str) -> dict:
        return {"html_url": str(self.patch_path), "number": 0}
