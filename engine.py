"""ci-red-to-green engine: watch a branch, turn a red run into an incident,
keep the proof ledger, gate the fix, build the PR and follow its CI.

Everything Hermes-specific (hooks, slash command, secrets, posting) lives in
__init__.py. This module takes plain callables so it can be tested without Hermes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import guard, ledger, logparse
from .github import GitHub, GitHubError

POLL_SECONDS = 30


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def blob_snapshot(root: str) -> dict[str, str]:
    """git blob sha of every file in the worktree (skipping caches), keyed by relative path."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in guard._SKIP_DIRS and not d.endswith(".egg-info")]
        for f in filenames:
            full = os.path.join(dirpath, f)
            if os.path.islink(full) or not os.path.isfile(full) or f.endswith((".pyc", ".pyo")):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as fh:
                out[rel] = git_blob_sha(fh.read())
    return out


@dataclass
class Incident:
    run_id: int
    run_number: int
    run_url: str
    head_sha: str
    branch: str
    test_ids: list[str]
    reasons: dict[str, str]
    command: str
    worktree: str
    status: str = "alerted"  # alerted -> fixing -> ready -> pr_open -> done | ci_red | discarded
    base_tree: dict[str, str] = field(default_factory=dict)  # path -> blob sha at head_sha
    runs: list[dict] = field(default_factory=list)  # serialised ledger runs
    approved_repro: bool = False
    pr_url: str = ""
    pr_branch: str = ""
    pr_sha: str = ""
    pr_run_url: str = ""

    def ledger(self) -> ledger.Ledger:
        lg = ledger.Ledger(self.test_ids)
        for r in self.runs:
            lg.runs.append(ledger.Run(**r))
        return lg


@dataclass
class Watch:
    repo: str
    branch: str
    seen: list[int] = field(default_factory=list)


class Engine:
    def __init__(self, data_dir: Path, token_fn: Callable[[], str | None],
                 post: Callable[[str], object], gh_factory=GitHub, clock=time.time):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._token_fn = token_fn
        self._post = post
        self._gh_factory = gh_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.watch: Watch | None = None
        self.incident: Incident | None = None
        self._load()

    # ---- persistence -----------------------------------------------------------
    @property
    def _state_file(self) -> Path:
        return self.data_dir / "state.json"

    def _load(self) -> None:
        try:
            raw = json.loads(self._state_file.read_text())
        except (OSError, ValueError):
            return
        if raw.get("watch"):
            self.watch = Watch(**raw["watch"])
        if raw.get("incident"):
            self.incident = Incident(**raw["incident"])

    def save(self) -> None:
        with self._lock:
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "watch": asdict(self.watch) if self.watch else None,
                "incident": asdict(self.incident) if self.incident else None,
            }))
            os.replace(tmp, self._state_file)

    # ---- github ----------------------------------------------------------------
    def gh(self) -> GitHub:
        token = self._token_fn()
        if not token:
            raise GitHubError(401, "CI_RED_TO_GREEN_GITHUB_TOKEN is not set")
        return self._gh_factory(token, self.watch.repo)

    # ---- watching ---------------------------------------------------------------
    def start_watch(self, repo: str, branch: str) -> str:
        with self._lock:
            self.watch = Watch(repo=repo, branch=branch)
            gh = self.gh()
            # Everything already finished counts as seen; only new red runs alert.
            self.watch.seen = [r["id"] for r in gh.runs(branch=branch) if r.get("status") == "completed"]
            self.save()
        return f"Watching {repo} @ {branch}. I'll message you here when CI goes red."

    def run_loop(self) -> None:
        """Poll until stopped. Meant to run in a thread started with copy_context()."""
        while not self._stop.is_set():
            try:
                self.tick()
            except GitHubError as e:
                if e.status in (401, 403, 404):
                    self._post(f"CI watch stopped: {e}")
                    return
            except Exception:  # never let one bad poll kill the watcher
                pass
            self._stop.wait(POLL_SECONDS)

    def start_thread(self, runner: Callable[[Callable[[], None]], None] | None = None) -> None:
        self.stop_thread()
        self._stop = threading.Event()
        target = (lambda: runner(self.run_loop)) if runner else self.run_loop
        self._thread = threading.Thread(target=target, name="ci-red-to-green", daemon=True)
        self._thread.start()

    def stop_thread(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        if not self.watch:
            return
        gh = self.gh()
        inc = self.incident
        if inc and inc.status == "pr_open":
            self._follow_pr(gh, inc)
        if inc and inc.status in ("alerted", "fixing", "ready", "pr_open"):
            return  # one incident at a time
        for run in gh.runs(branch=self.watch.branch):
            if run.get("status") != "completed" or run["id"] in self.watch.seen:
                continue
            self.watch.seen.append(run["id"])
            self.save()
            if run.get("conclusion") == "failure":
                self._open_incident(gh, run)
                return

    def _open_incident(self, gh: GitHub, run: dict) -> None:
        failure = logparse.Failure()
        for job in gh.failed_jobs(run["id"]):
            f = logparse.parse_log(gh.job_log(job["id"]))
            for t in f.test_ids:
                if t not in failure.test_ids:
                    failure.test_ids.append(t)
                    failure.reasons[t] = f.reasons.get(t, "")
        if not failure:
            self._post(f"CI red on {self.watch.branch} (run #{run.get('run_number')}), but I couldn't find a failing pytest test in the log, so there's nothing I can reproduce. {run.get('html_url', '')}")
            return
        wt = str(self.data_dir / "worktrees" / str(run["id"]))
        self.incident = Incident(
            run_id=run["id"], run_number=run.get("run_number", 0), run_url=run.get("html_url", ""),
            head_sha=run["head_sha"], branch=self.watch.branch, test_ids=failure.test_ids,
            reasons=failure.reasons, command=failure.command, worktree=wt,
        )
        self.save()
        first = failure.test_ids[0]
        more = f" (+{len(failure.test_ids) - 1} more)" if len(failure.test_ids) > 1 else ""
        why = failure.reasons.get(first, "")
        self._post(
            f"CI red on {self.watch.branch}: {first}{more}\n"
            f"{why}\n"
            f"run #{run.get('run_number')} · {run['head_sha'][:7]}\n"
            "Reply fix and I'll reproduce it and prove a fix before offering a PR."
        )

    # ---- the fix turn -------------------------------------------------------------
    def wants_fix(self, user_message: str) -> bool:
        inc = self.incident
        return bool(inc and inc.status in ("alerted", "fixing", "ci_red") and
                    re.fullmatch(r"\s*(fix|fix it|go|yes,? fix( it)?)[.!]?\s*", user_message or "", re.I))

    def fix_context(self) -> str:
        inc = self.incident
        inc.status = "fixing"
        self._ensure_base_tree()
        self.save()
        ids = "\n".join(f"  - {t}: {inc.reasons.get(t, '')}" for t in inc.test_ids)
        return (
            "[ci-red-to-green] The user asked you to fix a red CI run.\n"
            f"Repo {self.watch.repo}, branch {inc.branch}, commit {inc.head_sha}, run #{inc.run_number}.\n"
            f"Failing tests:\n{ids}\n\n"
            "Do exactly this, using the terminal tool:\n"
            f"1. If {inc.worktree} doesn't exist yet: git clone https://github.com/{self.watch.repo}.git {inc.worktree} && git -C {inc.worktree} checkout --detach {inc.head_sha}\n"
            f"2. In {inc.worktree}, install the project the way its CI does (for example python -m pip install -e . pytest), then reproduce BEFORE changing anything: {inc.command}\n"
            f"3. Run the whole suite once before changing anything: python -m pytest -rA\n"
            "4. Fix the cause in the source code. Do not edit tests, conftest.py, pytest/tox/setup config or .github: "
            "the PR is built from source files only, so test edits are dropped and the fix would be refused.\n"
            f"5. After the fix, run {inc.command} three times, then the whole suite once (python -m pytest -rA).\n"
            "6. Don't commit or push. Tell the user in one line what the cause was and to send /ci to see the proof and open the PR.\n"
            "Always pass -rA to pytest so results can be checked."
        )

    def _ensure_base_tree(self) -> None:
        inc = self.incident
        if inc.base_tree:
            return
        gh = self.gh()
        commit = gh._req("GET", f"/repos/{self.watch.repo}/git/commits/{inc.head_sha}")
        tree = gh._req("GET", f"/repos/{self.watch.repo}/git/trees/{commit['tree']['sha']}?recursive=1")
        inc.base_tree = {e["path"]: e["sha"] for e in tree.get("tree", []) if e.get("type") == "blob"}

    def _in_worktree(self, command: str, workdir: str | None) -> bool:
        wt = self.incident.worktree if self.incident else None
        return bool(wt and ((workdir or "").startswith(wt) or wt in (command or "")))

    def source_changes(self) -> tuple[dict[str, str], dict[str, str]]:
        inc = self.incident
        if not inc or not os.path.isdir(inc.worktree):
            return {}, {}
        ch = guard.changes(inc.base_tree, blob_snapshot(inc.worktree))
        return guard.split(ch)

    def needs_approval(self, tool_name: str, args: dict) -> str | None:
        """pre_tool_call: the first pytest run in the worktree goes through Hermes's approval prompt.
        Pushes from the worktree are blocked: the PR only goes out through /ci."""
        inc = self.incident
        if not inc or inc.status != "fixing" or tool_name != "terminal":
            return None
        cmd = args.get("command", "")
        if not self._in_worktree(cmd, args.get("workdir")):
            return None
        if re.search(r"\bgit\b[^;&|]*\bpush\b|\bgh\s+pr\b", cmd):
            return "block"
        if "pytest" in cmd and not inc.approved_repro:
            inc.approved_repro = True
            self.save()
            return "approve"
        return None

    def observe(self, tool_name: str, args: dict, result: str) -> None:
        """post_tool_call: record pytest output from Hermes's own terminal results."""
        inc = self.incident
        if not inc or inc.status not in ("fixing", "ready") or tool_name != "terminal":
            return
        cmd = args.get("command", "")
        if "pytest" not in cmd or not self._in_worktree(cmd, args.get("workdir")):
            return
        try:
            body = json.loads(result)
            output, code = body.get("output", ""), body.get("exit_code")
        except (ValueError, TypeError, AttributeError):
            output, code = str(result), None
        src, _prot = self.source_changes()
        lg = inc.ledger()
        run = lg.record(output, code, changed=bool(src))
        if run is None:
            return
        run.full_suite = len(run.results) > len(inc.test_ids)
        inc.runs.append(asdict(run))
        if inc.ledger().verdict()[0]:
            inc.status = "ready"
        self.save()

    # ---- the card -------------------------------------------------------------------
    def proof(self) -> dict:
        inc = self.incident
        lg = inc.ledger()
        summ = lg.summary()
        src, prot = self.source_changes()
        ok = summ["accepted"] and bool(src) and not prot
        reasons = list(summ["reasons"])
        if not src:
            reasons.append("no source change in the worktree")
        if prot:
            reasons.append("test or CI files changed: " + ", ".join(sorted(prot)))
        return {"accepted": ok, "reasons": reasons, "source": src, "protected": prot, **{k: summ[k] for k in ("tests", "suite")}}

    def card_text(self) -> str:
        inc = self.incident
        p = self.proof()
        lines = [f"Fix for run #{inc.run_number} on {inc.branch}"]
        for t, s in p["tests"].items():
            lines.append(f"{t}: {'FAILED before' if s['failed_before'] else 'not seen failing'} · {s['passes_after']}x PASSED after (needs {ledger.PASSES_NEEDED})")
        lines.append(f"suite: {p['suite']}")
        lines.append("test files untouched" if not p["protected"] else "test files changed: " + ", ".join(sorted(p["protected"])))
        lines.append("changed: " + (", ".join(f"{k} {path}" for path, k in sorted(p["source"].items())) or "nothing"))
        if not p["accepted"]:
            lines.append("Not ready: " + "; ".join(p["reasons"]))
        return "\n".join(lines)

    def pr_body(self) -> str:
        inc = self.incident
        p = self.proof()
        rows = "\n".join(f"| `{t}` | {'FAILED' if s['failed_before'] else '-'} | {s['passes_after']}x PASSED |" for t, s in p["tests"].items())
        return (
            f"CI went red on `{inc.branch}` at {inc.head_sha[:7]} ([run #{inc.run_number}]({inc.run_url})).\n\n"
            "This fix was checked locally by Hermes before this PR was opened. The results below come from pytest's own output, recorded by the plugin, not from the model:\n\n"
            "| test | before the fix | after the fix |\n|---|---|---|\n" + rows + "\n\n"
            f"Full suite: {p['suite']}. Test files untouched: this commit contains source files only, so CI here runs the original tests.\n\n"
            "Opened with ci-red-to-green for Hermes Agent. It never pushes to the base branch and never merges."
        )

    def open_pr(self) -> str:
        inc = self.incident
        p = self.proof()
        if not p["accepted"]:
            return "Not opening a PR: " + "; ".join(p["reasons"])
        files: dict[str, bytes | None] = {}
        for path, kind in p["source"].items():
            files[path] = None if kind == "deleted" else Path(inc.worktree, path).read_bytes()
        gh = self.gh()
        branch = f"ci-fix/run-{inc.run_number}-{int(self._clock())}"
        first = inc.test_ids[0].split("::")[-1]
        sha = gh.create_branch_commit(inc.head_sha, files, f"Fix {first} (CI run #{inc.run_number})", branch)
        pr = gh.open_pr(branch, inc.branch, f"Fix {first} (red CI run #{inc.run_number})", self.pr_body())
        inc.pr_url, inc.pr_branch, inc.pr_sha, inc.status = pr.get("html_url", ""), branch, sha, "pr_open"
        self.save()
        return f"PR opened: {inc.pr_url}\nI'll tell you here when GitHub's CI finishes on it."

    def _follow_pr(self, gh: GitHub, inc: Incident) -> None:
        runs = [r for r in gh.runs(head_sha=inc.pr_sha) if r.get("status") == "completed"]
        if not runs:
            return
        if all(r.get("conclusion") == "success" for r in runs):
            inc.status, inc.pr_run_url = "done", runs[0].get("html_url", "")
            self.save()
            self._post(f"Done. GitHub's CI is green on the fix: {inc.pr_run_url}\nPR: {inc.pr_url} (not merged; that's yours)")
        elif any(r.get("conclusion") == "failure" for r in runs):
            inc.status, inc.pr_run_url = "ci_red", runs[0].get("html_url", "")
            self.save()
            self._post(f"GitHub's CI is still red on the fix PR: {inc.pr_run_url}\nThe local proof didn't hold on CI. Reply fix to try again from the latest commit.")

    def discard(self) -> str:
        if self.incident:
            self.incident.status = "discarded"
            self.save()
        return "Discarded. Nothing was pushed."

    def status(self) -> str:
        if not self.watch:
            return "Not watching anything. Usage: /ci watch owner/repo [branch]"
        s = f"Watching {self.watch.repo} @ {self.watch.branch}."
        inc = self.incident
        if inc and inc.status not in ("done", "discarded"):
            s += f"\nIncident: run #{inc.run_number}, {', '.join(inc.test_ids)}, status {inc.status}."
            if inc.pr_url:
                s += f"\nPR: {inc.pr_url}"
        return s
