"""ci-red-to-green engine: watch a branch, turn a red run into an incident,
keep the proof ledger, gate the fix, build the PR and follow its CI.

Everything Hermes-specific (hooks, slash command, secrets, posting) lives in
__init__.py. This module takes plain callables so it can be tested without Hermes.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import demo, guard, ledger, logparse, runners
from .github import GitHub, GitHubError

POLL_SECONDS = 30


def _state_of(src: dict[str, str], snap: dict[str, str]) -> str:
    """A short hash of the source changes in the fix folder: which files changed and their content."""
    key = "\n".join(f"{p} {snap.get(p, 'deleted')}" for p in sorted(src))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# Commands that only read or fetch, and never run the repo's own code. Anything else in the
# fix folder (pytest, pip install, python, make, tox...) counts as running the repo's code.
_READ_ONLY = re.compile(
    r"^(?:cd|ls|cat|head|tail|grep|rg|wc|pwd|echo|printf|diff|tree|file|stat|true|test|\[|mkdir|which"
    r"|git\s+(?:-C\s+\S+\s+)?(?:clone|status|diff|log|show|fetch|checkout|switch|rev-parse|branch|ls-files|blame|stash))(?=\s|$)"
)


# find and sed only read unless asked to run or edit something
_READ_ONLY_IF = re.compile(r"^(?:find(?!.*\s-(?:exec|execdir|ok|okdir|delete|fprint)\b)|sed(?!.*\s-[a-zA-Z]*i)(?!.*\s--in-place))(?=\s|$)")


def runs_repo_code(command: str) -> bool:
    """True unless every part of the command is a known read-only step."""
    parts = [p.strip().strip("()").strip() for p in re.split(r"&&|\|\||;|\|", command or "")]
    return any(p and not (_READ_ONLY.match(p) or _READ_ONLY_IF.match(p)) for p in parts)


def _read_ignore(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n").rstrip() for ln in fh]
    except OSError:
        return []


def _rule_matches(pattern: str, rel: str) -> bool:
    """git-style match of one .gitignore line (already stripped of '!') against a path
    relative to that .gitignore's folder."""
    dir_only = pattern.endswith("/")
    p = pattern.strip("/")
    if not p:
        return False
    anchored = pattern.startswith("/") or "/" in p
    parts = rel.split("/")
    # every leading directory of the file, plus the file itself (directory rules skip the file)
    cands = ["/".join(parts[: i + 1]) for i in range(len(parts) - (1 if dir_only else 0))]
    if anchored:
        return any(fnmatch.fnmatchcase(c, p) or fnmatch.fnmatchcase(c, p.replace("**/", "")) for c in cands)
    names = parts[:-1] if dir_only else parts
    return any(fnmatch.fnmatchcase(n, p) for n in names)


def _ignored(rel: str, rules: list[tuple[str, str]]) -> bool:
    """rules: (folder of the .gitignore relative to the root, line). The last matching line wins."""
    hit = False
    for base, line in rules:
        if not line or line.startswith("#"):
            continue
        if base and not rel.startswith(base + "/"):
            continue
        sub = rel[len(base) + 1:] if base else rel
        neg = line.startswith("!")
        pat = line[1:] if neg else line
        if pat.startswith("\\"):
            pat = pat[1:]
        if _rule_matches(pat, sub):
            hit = not neg
    return hit


def blob_snapshot(root: str, tracked: dict[str, str] | None = None) -> dict[str, str]:
    """git blob sha of every file in the worktree, keyed by relative path. Caches and files
    matched by the repo's .gitignore files (nested ones and '!' lines included) are skipped,
    unless the base commit tracks them."""
    out: dict[str, str] = {}
    tracked = tracked or {}
    rules: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in guard._SKIP_DIRS and not d.endswith(".egg-info"))
        here = os.path.relpath(dirpath, root).replace(os.sep, "/")
        here = "" if here == "." else here
        if ".gitignore" in filenames:
            rules.extend((here, ln) for ln in _read_ignore(os.path.join(dirpath, ".gitignore")))
        for f in filenames:
            full = os.path.join(dirpath, f)
            if os.path.islink(full) or not os.path.isfile(full) or f.endswith((".pyc", ".pyo")):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel not in tracked and _ignored(rel, rules):
                continue
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
    last_green: str = ""  # head sha of the last green run before this one
    suspects: list[dict] = field(default_factory=list)  # commits since then: sha, title, author
    opened_at: float = 0.0
    runner: str = "pytest"
    repo: str = ""
    suspects_total: int = 0
    ignored_runs: int = 0
    anchor_blobs: dict[str, str] = field(default_factory=dict)  # source files changed since green -> blob at green

    def ledger(self) -> ledger.Ledger:
        lg = ledger.Ledger(self.test_ids, parse=runners.get(self.runner).parse_results)
        for r in self.runs:
            lg.runs.append(ledger.Run(**r))
        return lg


@dataclass
class Watch:
    repo: str
    branch: str
    seen: list[int] = field(default_factory=list)
    demo: bool = False


class Engine:
    def __init__(self, data_dir: Path, token_fn: Callable[[], str | None],
                 post: Callable[[str], object], gh_factory=GitHub, clock=time.time,
                 on_change: Callable[[dict], object] | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._token_fn = token_fn
        self._post = post
        self._gh_factory = gh_factory
        self._clock = clock
        self._on_change = on_change
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.watches: list[Watch] = []
        self.queue: list[dict] = []  # red runs waiting while another incident is open
        self.incident: Incident | None = None
        self.history: list[dict] = []
        self._blobs: dict[str, bytes] = {}
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
        self.watches = [Watch(**w) for w in raw.get("watches") or ([raw["watch"]] if raw.get("watch") else [])]
        self.queue = raw.get("queue") or []
        if raw.get("incident"):
            self.incident = Incident(**raw["incident"])
        self.history = raw.get("history") or []

    def save(self) -> None:
        with self._lock:
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "watches": [asdict(w) for w in self.watches],
                "queue": self.queue,
                "incident": asdict(self.incident) if self.incident else None,
                "history": self.history[:20],
            }))
            os.replace(tmp, self._state_file)
        if self._on_change:
            try:  # tells the Desktop half to refresh; never let it break the engine
                self._on_change({"status": self.incident.status if self.incident else None})
            except Exception:
                pass

    @property
    def watch(self) -> Watch | None:
        """The watch the current incident belongs to, else the first one."""
        inc = self.incident
        if inc and inc.repo:
            for w in self.watches:
                if w.repo == inc.repo:
                    return w
        return self.watches[0] if self.watches else None

    def _watch_for(self, repo: str) -> Watch | None:
        return next((w for w in self.watches if w.repo == repo), None)

    # ---- github ----------------------------------------------------------------
    def gh(self, repo: str | None = None) -> GitHub:
        w = self._watch_for(repo) if repo else self.watch
        if w and w.demo:
            return demo.DemoGitHub(None, w.repo, self.data_dir)
        token = self._token_fn()
        if not token:
            raise GitHubError(401, "CI_RED_TO_GREEN_GITHUB_TOKEN is not set")
        return self._gh_factory(token, repo or (w.repo if w else ""))

    # ---- watching ---------------------------------------------------------------
    def start_watch(self, repo: str, branch: str) -> str:
        """Add (or re-point) a watch. Several repos can be watched; incidents are fixed one at a time."""
        with self._lock:
            inc = self.incident
            active = inc and inc.status in ("alerted", "fixing", "ready", "ci_red")
            old = self._watch_for(repo)
            if active and (self.watch and self.watch.demo or (old and inc.repo == repo and old.branch != branch)):
                inc.status = "discarded"  # it belongs to the demo or to the old branch
            self.watches = [w for w in self.watches if not w.demo and w.repo != repo]
            self.queue = [q for q in self.queue if q["repo"] != repo]
            w = Watch(repo=repo, branch=branch)
            self.watches.append(w)
            gh = self.gh(repo)
            # Everything already finished counts as seen; only new red runs alert.
            w.seen = [r["id"] for r in gh.runs(branch=branch) if r.get("status") == "completed"]
            self.save()
        others = [f"{x.repo} @ {x.branch}" for x in self.watches if x.repo != repo]
        also = f" Also watching: {', '.join(others)}." if others else ""
        return f"Watching {repo} @ {branch}. I'll message you here when CI goes red.{also}"

    def unwatch(self, repo: str) -> str:
        with self._lock:
            if not self._watch_for(repo):
                return f"Not watching {repo}."
            if self.incident and self.incident.repo == repo and self.incident.status in ("alerted", "fixing", "ready", "ci_red"):
                self.incident.status = "discarded"
            self.watches = [w for w in self.watches if w.repo != repo]
            self.queue = [q for q in self.queue if q["repo"] != repo]
            self.save()
        return f"Stopped watching {repo}."

    def _archive(self) -> None:
        """Keep a short record of the incident being replaced, for the Desktop history page."""
        inc = self.incident
        if not inc:
            return
        self.history.insert(0, {
            "repo": inc.repo or (self.watch.repo if self.watch else ""), "run_number": inc.run_number, "run_url": inc.run_url,
            "branch": inc.branch, "sha": inc.head_sha[:7], "tests": len(inc.test_ids), "first_test": inc.test_ids[0] if inc.test_ids else "",
            "status": inc.status, "pr_url": inc.pr_url, "pr_run_url": inc.pr_run_url, "opened_at": inc.opened_at,
            "suspects": inc.suspects[:1],
        })
        del self.history[20:]

    def start_demo(self) -> str:
        """Replay a real bug from humanize on a bundled copy, with no token and no network."""
        with self._lock:
            if self.incident and self.incident.status not in ("done", "discarded"):
                self.incident.status = "discarded"
            self.watches = [Watch(repo=demo.REPO, branch="main", demo=True)]
            self.queue = []
            demo.copy_sample(str(self.data_dir / "worktrees" / str(demo.RUN_ID)))
            self.save()
            self.tick()
        return ("Demo: a copy of humanize's filesize code with the real bug humanize fixed in PR #329, "
                "and pytest's real output on it as the red CI log. No token, no network, no PR: Open PR writes a patch file. "
                "Run /ci watch owner/repo afterwards to go back to a real repo.")

    def run_loop(self) -> None:
        """Poll until stopped. Meant to run in a thread started with copy_context()."""
        stop = self._stop  # this thread's own event: re-arming swaps self._stop, and the old loop must end
        while not stop.is_set():
            try:
                with self._lock:
                    self.tick()
            except GitHubError as e:
                if e.status in (401, 403, 404):
                    self._post(f"CI watch stopped: {e}")
                    return
            except Exception:  # never let one bad poll kill the watcher
                logging.getLogger(__name__).exception("ci-red-to-green: poll failed")
            stop.wait(POLL_SECONDS)

    def start_thread(self, runner: Callable[[Callable[[], None]], None] | None = None) -> None:
        self.stop_thread()
        self._stop = threading.Event()
        target = (lambda: runner(self.run_loop)) if runner else self.run_loop
        self._thread = threading.Thread(target=target, name="ci-red-to-green", daemon=True)
        self._thread.start()

    def thread_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def stop_thread(self) -> None:
        self._stop.set()

    def _busy(self) -> bool:
        inc = self.incident
        return bool(inc and inc.status in ("alerted", "fixing", "ready", "pr_open"))

    def tick(self) -> None:
        if not self.watches:
            return
        inc = self.incident
        if inc and inc.status == "pr_open":
            self._follow_pr(self.gh(inc.repo or None), inc)
        for w in list(self.watches):
            try:
                self._tick_watch(w)
            except GitHubError as e:
                if e.status in (401, 403, 404) and not w.demo:
                    # this repo's access is gone: stop watching it, keep the others
                    self.watches = [x for x in self.watches if x.repo != w.repo]
                    self.save()
                    self._post(f"Stopped watching {w.repo}: {e}")
                else:
                    raise
        self._drain_queue()

    def _tick_watch(self, w: Watch) -> None:
        gh = self.gh(w.repo)
        for run in gh.runs(branch=w.branch):
            if run.get("status") != "completed" or run["id"] in w.seen:
                continue
            w.seen.append(run["id"])
            del w.seen[:-200]
            self.save()
            if run.get("conclusion") != "failure":
                continue
            if not self._busy():
                self._open_incident(gh, run, w)
            elif self.incident.repo != w.repo:
                # one fix at a time: another repo's red run waits its turn
                self.queue.append({"repo": w.repo, "branch": w.branch, "run": run})
                self.save()
                self._post(f"Also red: {w.repo} @ {w.branch} (run #{run.get('run_number')}). "
                           f"Queued; I'll pick it up when the current fix for {self.incident.repo} is done or discarded.")
            # a newer red run on the repo being fixed is the same incident, so it's skipped

    def _drain_queue(self) -> None:
        while not self._busy() and self.queue:
            q = self.queue.pop(0)
            w = self._watch_for(q["repo"])
            if w:
                self._open_incident(self.gh(w.repo), q["run"], w)
            self.save()

    def _suspects(self, gh: GitHub, run: dict, w: Watch) -> tuple[str, list[dict]]:
        """The last green commit before this red run, and the commits that came in since."""
        try:
            runs = gh.runs(branch=w.branch, per_page=30)
            ids = [r["id"] for r in runs]
            older = runs[ids.index(run["id"]) + 1:] if run["id"] in ids else []
            green = next((r for r in older if r.get("status") == "completed" and r.get("conclusion") == "success"
                          and r.get("head_sha") != run["head_sha"]
                          and r.get("workflow_id", run.get("workflow_id")) == run.get("workflow_id")), None)
            if not green:
                return "", []
            commits = gh.compare(green["head_sha"], run["head_sha"])
        except Exception:
            return "", []
        out = [{"sha": c.get("sha", ""), "title": (c.get("commit", {}).get("message") or "").splitlines()[0][:100] if c.get("commit") else "",
                "author": (c.get("author") or {}).get("login") or (c.get("commit", {}).get("author") or {}).get("name", "")}
               for c in commits]
        self._suspects_total = len(out)
        try:  # the source files that changed since green, as they were at green: the regression anchor
            files = [f for f in gh.compare_files(green["head_sha"], run["head_sha"]) if not guard.is_protected(f)]
            if files and len(files) <= 20:
                at_green = gh.tree(green["head_sha"])
                self._anchor = {f: at_green[f] for f in files if f in at_green}
        except Exception:
            self._anchor = {}
        return green["head_sha"], out[-5:]

    def _suspect_line(self, inc: Incident) -> str:
        if not inc.suspects:
            return ""
        if len(inc.suspects) == 1:
            c = inc.suspects[0]
            return f'Since the last green run: {c["sha"][:7]} "{c["title"]}"' + (f' by {c["author"]}' if c["author"] else "")
        return f"Since the last green run ({inc.last_green[:7]}): {max(inc.suspects_total, len(inc.suspects))} commits, newest {inc.suspects[-1]['sha'][:7]} \"{inc.suspects[-1]['title']}\""

    def _open_incident(self, gh: GitHub, run: dict, w: Watch) -> None:
        failure = logparse.Failure()
        for job in gh.failed_jobs(run["id"]):
            f = runners.parse_any(gh.job_log(job["id"]))
            if f and not failure:
                failure.runner = f.runner
            if f.runner != failure.runner:
                continue  # one runner per incident
            for t in f.test_ids:
                if t not in failure.test_ids:
                    failure.test_ids.append(t)
                    failure.reasons[t] = f.reasons.get(t, "")
        if not failure:
            self._post(f"CI red on {w.repo} @ {w.branch} (run #{run.get('run_number')}), but I couldn't find a failing pytest or go test in the log, so there's nothing I can reproduce. {run.get('html_url', '')}")
            return
        wt = str(self.data_dir / "worktrees" / str(run["id"]))
        self._suspects_total = 0
        self._anchor = {}
        green, suspects = self._suspects(gh, run, w)
        self._archive()
        self.incident = Incident(
            run_id=run["id"], run_number=run.get("run_number", 0), run_url=run.get("html_url", ""),
            head_sha=run["head_sha"], branch=w.branch, repo=w.repo, test_ids=failure.test_ids,
            reasons=failure.reasons, command=runners.get(failure.runner).command(failure.test_ids), worktree=wt,
            last_green=green, suspects=suspects, suspects_total=self._suspects_total, anchor_blobs=self._anchor, opened_at=self._clock(), runner=failure.runner,
        )
        self.save()
        first = failure.test_ids[0]
        more = f" (+{len(failure.test_ids) - 1} more)" if len(failure.test_ids) > 1 else ""
        why = failure.reasons.get(first, "")
        sus = self._suspect_line(self.incident)
        self._post(
            f"CI red on {w.branch}{'' if len(self.watches) < 2 else ' of ' + w.repo}: {first}{more}\n"
            f"{why}\n"
            f"run #{run.get('run_number')} · {run['head_sha'][:7]}\n"
            + (sus + "\n" if sus else "")
            + "Reply fix and I'll reproduce it and prove a fix before offering a PR."
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
        clue = ""
        if inc.suspects:
            listing = "\n".join(f"  - {c['sha'][:7]} {c['title']}" for c in inc.suspects)
            clue = (f"The last green commit was {inc.last_green[:7]}. These commits came in since, so the cause is most likely in one of them:\n{listing}\n"
                    f"Read what they changed first: git -C {inc.worktree} diff {inc.last_green} {inc.head_sha} -- . ':(exclude)tests'\n\n")
        rn = runners.get(inc.runner)
        if inc.anchor_blobs:
            files = " ".join(sorted(inc.anchor_blobs))
            clue += (f"Then confirm it's a regression from those commits, before fixing anything: restore only the changed source files to the last green commit "
                     f"(git -C {inc.worktree} checkout {inc.last_green} -- {files}), run {inc.command} once as its own terminal call "
                     f"(it should pass there), then put them back (git -C {inc.worktree} checkout {inc.head_sha} -- {files}).\n\n")
        install = ("2. Nothing to install beyond pytest. " if self.watch.demo else
                   f"2. In {inc.worktree}, install the project the way its CI does ({rn.install_hint}), then ")
        return (
            "[ci-red-to-green] The user asked you to fix a red CI run.\n"
            f"Repo {self.watch.repo}, branch {inc.branch}, commit {inc.head_sha}, run #{inc.run_number}.\n"
            f"Failing tests:\n{ids}\n\n" + clue +
            "Do exactly this, using the terminal tool:\n"
            f"1. If {inc.worktree} doesn't exist yet: git clone https://github.com/{self.watch.repo}.git {inc.worktree} && git -C {inc.worktree} checkout --detach {inc.head_sha}\n" +
            install + f"Reproduce BEFORE changing anything, twice, as two separate terminal calls in {inc.worktree}: {inc.command}\n"
            f"3. Run the whole suite once before changing anything: {rn.full_command}\n"
            "4. Fix the cause in the source code. Do not edit tests (test files, testdata, conftest.py), pytest/tox/setup config or .github: "
            "the PR is built from source files only, so test edits are dropped and the fix would be refused.\n"
            f"5. After the fix, run {inc.command} three times, then the whole suite once ({rn.full_command}).\n"
            "6. Don't commit or push. Tell the user in one line what the cause was and to send /ci to see the proof and open the PR.\n"
            f"{rn.flags_note}, and run each test command as its own terminal call, with only cd, source or variables before it and nothing after it (no ;, &&, loops or pipes such as | tail), so every run is recorded."
        )

    def _ensure_base_tree(self) -> None:
        inc = self.incident
        if inc.base_tree:
            return
        inc.base_tree = self.gh().tree(inc.head_sha)

    def _in_worktree(self, command: str, workdir: str | None) -> bool:
        wt = self.incident.worktree.rstrip("/") if self.incident else ""
        if not wt:
            return False
        wd = (workdir or "").rstrip("/")
        return wd == wt or wd.startswith(wt + "/") or bool(re.search(re.escape(wt) + r"(?:/|\s|['\"]|$)", command or ""))

    def source_changes(self) -> tuple[dict[str, str], dict[str, str]]:
        inc = self.incident
        if not inc or not os.path.isdir(inc.worktree):
            return {}, {}
        ch = guard.changes(inc.base_tree, blob_snapshot(inc.worktree, inc.base_tree))
        return guard.split(ch)

    def needs_approval(self, tool_name: str, args: dict) -> str | None:
        """pre_tool_call: the first command that runs the repo's code in the worktree (pytest, an
        install, python...) goes through Hermes's approval prompt; read-only steps don't.
        Pushes from the worktree are blocked: the PR only goes out through /ci."""
        inc = self.incident
        if not inc or inc.status != "fixing" or tool_name != "terminal":
            return None
        cmd = args.get("command", "")
        if not self._in_worktree(cmd, args.get("workdir")):
            return None
        if re.search(r"\bgit\b[^;&|]*\bpush\b|\bgh\s+pr\b", cmd):
            return "block"
        if runs_repo_code(cmd) and not inc.approved_repro:
            inc.approved_repro = True  # observe() undoes this if the user denies or the prompt times out
            self.save()
            return "approve"
        return None

    def observe(self, tool_name: str, args: dict, result: str) -> None:
        """post_tool_call: record test-runner output from Hermes's own terminal results."""
        inc = self.incident
        if not inc or inc.status not in ("fixing", "ready") or tool_name != "terminal":
            return
        cmd = args.get("command", "")
        rn = runners.get(inc.runner)
        if inc.approved_repro and self._in_worktree(cmd, args.get("workdir")) and runs_repo_code(cmd) \
                and "BLOCKED:" in str(result)[:400]:
            # Hermes refused it (denied, or the prompt timed out): ask again next time
            inc.approved_repro = False
            self.save()
            return
        if not rn.is_cmd(cmd) or not self._in_worktree(cmd, args.get("workdir")):
            return
        try:
            body = json.loads(result)
            output, code = body.get("output", ""), body.get("exit_code")
        except (ValueError, TypeError, AttributeError):
            output, code = str(result), None
        snap = blob_snapshot(inc.worktree, inc.base_tree) if os.path.isdir(inc.worktree) else {}
        src, prot = guard.split(guard.changes(inc.base_tree, snap)) if snap else ({}, {})
        if prot:
            # a run made while tests or test config were edited proves nothing, even if the edit is undone later
            inc.ignored_runs += 1
            self.save()
            return
        lg = inc.ledger()
        sessions = rn.split_sessions(output)
        for chunk in sessions:
            if not rn.is_real_session(chunk):
                continue
            # every changed source file holds exactly its last-green content: a run at the regression anchor
            anchor = bool(src) and bool(inc.anchor_blobs) and set(src) == set(inc.anchor_blobs) and \
                all(snap.get(f) == sha for f, sha in inc.anchor_blobs.items())
            # only once, and only before any fix run: a fix that restores the green code is a fix, not an anchor
            anchor = anchor and not any(r.anchor or r.changed for r in lg.runs)
            run = lg.record(chunk, code if len(sessions) == 1 else None, changed=bool(src) and not anchor, anchor=anchor,
                            state=_state_of(src, snap))
            if run is None:
                continue
            run.full_suite = rn.is_full(cmd)
            inc.runs.append(asdict(run))
        if inc.ledger().verdict(_state_of(src, snap) if src else None, need_green=bool(inc.anchor_blobs))[0]:
            inc.status = "ready"
        self.save()

    def diff(self, max_lines: int = 60) -> str:
        """Unified diff of the source files that would go into the PR, against the red commit."""
        inc = self.incident
        src, _ = self.source_changes()
        if not src:
            return ""
        out: list[str] = []
        gh = None
        for path, kind in sorted(src.items()):
            old = b""
            sha = inc.base_tree.get(path)
            if sha:
                if sha not in self._blobs:
                    gh = gh or self.gh()
                    try:
                        self._blobs[sha] = gh.blob(sha)
                    except Exception:
                        self._blobs[sha] = b""
                old = self._blobs[sha]
            new = b"" if kind == "deleted" else Path(inc.worktree, path).read_bytes()
            out.extend(difflib.unified_diff(old.decode("utf-8", "replace").splitlines(), new.decode("utf-8", "replace").splitlines(),
                                            f"a/{path}", f"b/{path}", lineterm="", n=1))
        if len(out) > max_lines:
            out = out[:max_lines] + [f"... ({len(out) - max_lines} more lines)"]
        return "\n".join(out)

    # ---- the card -------------------------------------------------------------------
    def proof(self) -> dict:
        inc = self.incident
        lg = inc.ledger()
        snap = blob_snapshot(inc.worktree, inc.base_tree) if os.path.isdir(inc.worktree) else {}
        src, prot = guard.split(guard.changes(inc.base_tree, snap)) if snap else ({}, {})
        summ = lg.summary(_state_of(src, snap) if src else None, need_green=bool(inc.anchor_blobs))
        ok = summ["accepted"] and bool(src) and not prot
        reasons = list(summ["reasons"])
        if not src:
            reasons.append("no source change in the worktree")
        if prot:
            reasons.append("test or CI files changed: " + ", ".join(sorted(prot)))
        return {"accepted": ok, "reasons": reasons, "source": src, "protected": prot, "ignored_runs": inc.ignored_runs,
                **{k: summ[k] for k in ("tests", "suite")}}

    def card_text(self) -> str:
        inc = self.incident
        p = self.proof()
        lines = [f"Fix for run #{inc.run_number} on {inc.branch}"]
        for t, s in p["tests"].items():
            before = ("flaky: passed before any change" if s["flaky"] else
                      f"FAILED before the fix {min(s['fails_before'], ledger.FAILS_NEEDED)}/{ledger.FAILS_NEEDED}")
            if s.get("at_green"):
                before = f"{s['at_green']} at last green {inc.last_green[:7]} · " + before
            lines.append(f"{t}: {before} · PASSED after it {min(s['passes_after'], ledger.PASSES_NEEDED)}/{ledger.PASSES_NEEDED} {'✓' if s['passes_after'] >= ledger.PASSES_NEEDED else ''}".rstrip())
        lines.append(f"suite: {p['suite']}")
        lines.append("test files untouched" if not p["protected"] else "test files changed: " + ", ".join(sorted(p["protected"])))
        lines.append("changed: " + (", ".join(f"{k} {path}" for path, k in sorted(p["source"].items())) or "nothing"))
        sus = self._suspect_line(inc)
        if sus:
            lines.append(sus)
        d = self.diff(30)
        if d:
            lines.append("")
            lines.append(d)
        if p["ignored_runs"]:
            lines.append(f"{p['ignored_runs']} run(s) made while test files were edited were not counted")
        if not p["accepted"]:
            lines.append("Not ready: " + "; ".join(p["reasons"]))
        return "\n".join(lines)

    def pr_body(self) -> str:
        inc = self.incident
        p = self.proof()
        anchored = any(s.get("at_green") for s in p["tests"].values())
        green_col = f" at the last green commit ({inc.last_green[:7]}) |" if anchored else ""
        rows = "\n".join(f"| `{t}` |" + (f" {s.get('at_green') or '-'} |" if anchored else "")
                         + f" FAILED {min(s['fails_before'], ledger.FAILS_NEEDED)}/{ledger.FAILS_NEEDED} runs | PASSED {min(s['passes_after'], ledger.PASSES_NEEDED)}/{ledger.PASSES_NEEDED} runs in a row |" for t, s in p["tests"].items())
        sus = ""
        if inc.suspects:
            sus = "Commits since the last green run (" + inc.last_green[:7] + "): " + ", ".join(f"{c['sha'][:7]} {c['title']}" for c in inc.suspects) + ".\n\n"
        return (
            f"CI went red on `{inc.branch}` at {inc.head_sha[:7]} ([run #{inc.run_number}]({inc.run_url})).\n\n"
            f"This fix was checked locally by Hermes before this PR was opened. The results below come from the test runner's own output ({runners.get(inc.runner).name}), recorded by the plugin, not from the model:\n\n"
            "| test |" + green_col + " before the fix | after the fix |\n|---|" + ("---|" if anchored else "") + "---|---|\n" + rows + "\n\n"
            + sus +
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
        try:
            sha = gh.create_branch_commit(inc.head_sha, files, f"Fix {first} (CI run #{inc.run_number})", branch)
            pr = gh.open_pr(branch, inc.branch, f"Fix {first} (red CI run #{inc.run_number})", self.pr_body())
        except GitHubError as e:
            if e.status not in (403, 404):
                raise
            # A read-only token can still watch and prove; hand over the proven patch instead of a PR.
            path = self._write_patch(files, f"Fix {first} (CI run #{inc.run_number})")
            inc.pr_url, inc.status = str(path), "done"
            self.save()
            return (f"Your token can't push to {self.watch.repo}, so here is the proven fix as a patch: {path}\n"
                    f"Apply it with: git apply {path}\nThe proof is in the patch header. Nothing was pushed.")
        inc.pr_url, inc.pr_branch, inc.pr_sha, inc.status = pr.get("html_url", ""), branch, sha, "pr_open"
        if self.watch.demo:
            inc.status = "done"
            self.save()
            return (f"Demo: no GitHub here, so the commit was written as a patch: {inc.pr_url}\n"
                    "With a real repo, this opens a PR with source files only and waits for GitHub's CI to go green.")
        self.save()
        return f"PR opened: {inc.pr_url}\nI'll tell you here when GitHub's CI finishes on it."

    def _write_patch(self, files: dict[str, bytes | None], subject: str) -> Path:
        inc = self.incident
        gh = self.gh()
        chunks = [f"Subject: {subject}\n\n" + self.pr_body() + "\n\n"]
        for path, new in sorted(files.items()):
            old = b""
            sha = inc.base_tree.get(path)
            if sha:
                old = self._blobs.get(sha) or gh.blob(sha)
            chunks.extend(difflib.unified_diff(old.decode("utf-8", "replace").splitlines(keepends=True),
                                               (new or b"").decode("utf-8", "replace").splitlines(keepends=True),
                                               f"a/{path}", f"b/{path}"))
        out = self.data_dir / f"fix-run-{inc.run_number}.patch"
        out.write_text("".join(chunks))
        return out

    def _follow_pr(self, gh: GitHub, inc: Incident) -> None:
        every = gh.runs(head_sha=inc.pr_sha)
        runs = [r for r in every if r.get("status") == "completed"]
        if not runs or (len(runs) < len(every) and not any(r.get("conclusion") == "failure" for r in runs)):
            return  # wait until every workflow on the fix has finished (a failure can be reported at once)
        if len(runs) == len(every) and not any(r.get("conclusion") in ("failure", "timed_out", "action_required") for r in runs) \
                and not any(r.get("conclusion") == "success" for r in runs):
            inc.status, inc.pr_run_url = "ci_red", runs[0].get("html_url", "")
            self.save()
            self._post(f"GitHub's checks on the fix PR ended without a result (skipped or cancelled): {inc.pr_run_url}\nPR: {inc.pr_url}")
            return
        if all(r.get("conclusion") in ("success", "skipped", "neutral") for r in runs):
            inc.status, inc.pr_run_url = "done", runs[0].get("html_url", "")
            self.save()
            self._post(f"Done. GitHub's CI is green on the fix: {inc.pr_run_url}\nPR: {inc.pr_url} (not merged; that's yours)")
        elif any(r.get("conclusion") == "failure" for r in runs):
            inc.status, inc.pr_run_url = "ci_red", runs[0].get("html_url", "")
            self.save()
            self._post(f"GitHub's CI is still red on the fix PR: {inc.pr_run_url}\nThe local proof didn't hold on CI. Reply fix to try again from the latest commit.")

    def snapshot(self) -> dict:
        """Everything the Desktop pane shows, as plain JSON."""
        inc = self.incident
        out: dict = {"watch": {"repo": self.watch.repo, "branch": self.watch.branch} if self.watch else None, "watching": self.thread_alive(), "incident": None}
        if inc and inc.status != "discarded":
            p = self.proof() if inc.status in ("fixing", "ready") else None
            out["incident"] = {
                "status": inc.status, "run_number": inc.run_number, "run_url": inc.run_url, "branch": inc.branch,
                "sha": inc.head_sha[:7], "tests": inc.test_ids, "reasons": inc.reasons,
                "proof": p and {"accepted": p["accepted"], "reasons": p["reasons"], "tests": p["tests"], "suite": p["suite"],
                                  "source": p["source"], "protected": p["protected"], "needed": ledger.PASSES_NEEDED, "needed_before": ledger.FAILS_NEEDED},
                "pr_url": inc.pr_url, "pr_run_url": inc.pr_run_url,
                "suspects": inc.suspects, "last_green": inc.last_green[:7],
                "diff": self.diff(80) if inc.status in ("fixing", "ready", "pr_open", "done") and os.path.isdir(inc.worktree) else "",
            }
        out["demo"] = bool(self.watch and self.watch.demo)
        out["watches"] = [{"repo": w.repo, "branch": w.branch} for w in self.watches]
        out["queue"] = [{"repo": q["repo"], "branch": q["branch"], "run_number": q["run"].get("run_number"),
                         "run_url": q["run"].get("html_url", "")} for q in self.queue]
        out["history"] = self.history[:20]
        return out

    def discard(self) -> str:
        if self.incident:
            self.incident.status = "discarded"
            self.save()
        nxt = f" Next in the queue: {self.queue[0]['repo']} (run #{self.queue[0]['run'].get('run_number')})." if self.queue else ""
        return "Discarded. Nothing was pushed." + nxt

    def status(self) -> str:
        if not self.watch:
            return "Not watching anything. Usage: /ci watch owner/repo [branch]"
        s = "Watching " + ", ".join(f"{w.repo} @ {w.branch}" for w in self.watches) + "."
        inc = self.incident
        if inc and inc.status not in ("done", "discarded"):
            s += f"\nIncident: run #{inc.run_number}, {', '.join(inc.test_ids)}, status {inc.status}."
            if inc.pr_url:
                s += f"\nPR: {inc.pr_url}"
        if self.queue:
            s += "\nQueued: " + ", ".join(f"{q['repo']} run #{q['run'].get('run_number')}" for q in self.queue)
        return s
