import json

from crg import engine as E

T = "tests/test_dates.py::test_parse_iso"
LOG = f"2026-11-11T06:01:05Z FAILED {T} - AssertionError: assert 11 == 12\n"
BASE = {"src/dates.py": b"def month(s):\n    return int(s[5:7]) - 1\n", "tests/test_dates.py": b"def test_parse_iso(): ...\n"}


class FakeGH:
    def __init__(self, token, repo):
        self.repo = repo
        self.state = FakeGH.state

    def runs(self, branch=None, head_sha=None, per_page=10):
        if head_sha:
            return self.state["pr_runs"]
        return self.state["runs"]

    def failed_jobs(self, run_id):
        return [{"id": 7}]

    def job_log(self, job_id):
        return LOG

    def _req(self, method, path, body=None, raw=False):
        if "/git/commits/" in path:
            return {"tree": {"sha": "t0"}}
        if "/git/trees/" in path:
            return {"tree": [{"path": p, "type": "blob", "sha": E.git_blob_sha(b)} for p, b in BASE.items()]}
        raise AssertionError(path)

    def create_branch_commit(self, base_sha, files, message, branch):
        self.state["commit"] = (base_sha, files, message, branch)
        return "f" * 40

    def open_pr(self, head, base, title, body):
        self.state["pr"] = (head, base, title, body)
        return {"html_url": "https://github.com/o/r/pull/1"}


def make(tmp_path):
    FakeGH.state = {"runs": [], "pr_runs": []}
    posts = []
    eng = E.Engine(tmp_path / "data", token_fn=lambda: "tok", post=posts.append, gh_factory=FakeGH, clock=lambda: 1000)
    return eng, posts


def write_worktree(eng, files):
    wt = eng.incident.worktree
    for p, b in files.items():
        f = __import__("pathlib").Path(wt, p)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b)


HEAD = "============================= test session starts ==============================\n"


def framed(output):
    n = output.count("PASSED")
    f = output.count("FAILED")
    return HEAD + output + f"========================= {f} failed, {n} passed in 0.04s =========================\n"


def term(eng, cmd, output, code, raw=False):
    output = output if raw else framed(output)
    args = {"command": cmd, "workdir": eng.incident.worktree}
    eng.observe("terminal", args, json.dumps({"output": output, "exit_code": code, "error": None}))


def red_run(n=12):
    return {"id": 100 + n, "run_number": n, "status": "completed", "conclusion": "failure",
            "head_sha": "a" * 40, "html_url": f"https://github.com/o/r/actions/runs/{100 + n}"}


def test_full_loop(tmp_path):
    eng, posts = make(tmp_path)
    FakeGH.state["runs"] = [{"id": 1, "status": "completed", "conclusion": "success", "head_sha": "0" * 40}]
    eng.start_watch("o/r", "main")
    eng.tick()
    assert posts == []  # old green run is not news

    FakeGH.state["runs"] = [red_run()] + FakeGH.state["runs"]
    eng.tick()
    assert "CI red on main: " + T in posts[-1] and "Reply fix" in posts[-1]
    assert eng.wants_fix("fix") and not eng.wants_fix("what broke?")

    ctx = eng.fix_context()
    assert "python -m pytest -rA " + T in ctx and "Do not edit tests" in ctx
    write_worktree(eng, BASE)

    # first pytest run in the worktree is escalated to Hermes's approval prompt, once
    a = {"command": f"cd {eng.incident.worktree} && python -m pytest -rA {T}"}
    assert eng.needs_approval("terminal", a) == "approve"
    assert eng.needs_approval("terminal", a) is None
    assert eng.needs_approval("terminal", {"command": f"git -C {eng.incident.worktree} push"}) == "block"

    term(eng, "python -m pytest -rA " + T, f"FAILED {T} - AssertionError\n", 1)
    term(eng, "python -m pytest -rA", f"FAILED {T}\nPASSED tests/test_dates.py::test_other\n", 1)
    write_worktree(eng, {"src/dates.py": b"def month(s):\n    return int(s[5:7])\n"})
    for _ in range(3):
        term(eng, "python -m pytest -rA " + T, f"PASSED {T}\n", 0)
    assert eng.incident.status == "fixing" or eng.proof()["suite"] == "not run"
    term(eng, "python -m pytest -rA", f"PASSED {T}\nPASSED tests/test_dates.py::test_other\n", 0)

    p = eng.proof()
    assert p["accepted"], p["reasons"]
    assert p["suite"] == "no new failures" and p["source"] == {"src/dates.py": "modified"}
    card = eng.card_text()
    assert "FAILED before the fix" in card and "PASSED after it 3/3 ✓" in card and "modified src/dates.py" in card and "test files untouched" in card

    out = eng.open_pr()
    assert out.startswith("PR opened: https://github.com/o/r/pull/1")
    base_sha, files, _msg, branch = FakeGH.state["commit"]
    assert base_sha == "a" * 40 and list(files) == ["src/dates.py"] and branch.startswith("ci-fix/run-12-")
    assert "FAILED" in FakeGH.state["pr"][3] and "never pushes" in FakeGH.state["pr"][3]

    FakeGH.state["pr_runs"] = [{"status": "completed", "conclusion": "success", "html_url": "https://ci/green"}]
    eng.tick()
    assert posts[-1].startswith("Done. GitHub's CI is green") and eng.incident.status == "done"


def test_test_edit_is_refused(tmp_path):
    eng, posts = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    eng.fix_context()
    write_worktree(eng, BASE)
    term(eng, "python -m pytest -rA " + T, f"FAILED {T}\n", 1)
    # the "fix" weakens the test
    write_worktree(eng, {"tests/test_dates.py": b"def test_parse_iso(): pass\n", "src/dates.py": b"x = 1\n"})
    for _ in range(3):
        term(eng, "python -m pytest -rA " + T, f"PASSED {T}\n", 0)
    p = eng.proof()
    assert not p["accepted"]
    assert any("test or CI files changed: tests/test_dates.py" in r for r in p["reasons"])
    assert eng.open_pr().startswith("Not opening a PR")
    assert "commit" not in FakeGH.state


def test_runs_outside_the_worktree_are_ignored(tmp_path):
    eng, _ = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    eng.fix_context()
    eng.observe("terminal", {"command": "python -m pytest -rA " + T, "workdir": "/elsewhere"},
                json.dumps({"output": f"PASSED {T}\n", "exit_code": 0}))
    assert eng.incident.runs == []


def test_red_pr_ci_is_reported(tmp_path):
    eng, posts = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    eng.incident.status, eng.incident.pr_sha, eng.incident.pr_url = "pr_open", "f" * 40, "u"
    FakeGH.state["pr_runs"] = [{"status": "completed", "conclusion": "failure", "html_url": "https://ci/red"}]
    eng.tick()
    assert "still red" in posts[-1] and eng.incident.status == "ci_red"
    assert eng.wants_fix("fix")


def test_state_survives_restart(tmp_path):
    eng, _ = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    again = E.Engine(tmp_path / "data", token_fn=lambda: "tok", post=lambda t: None, gh_factory=FakeGH)
    assert again.incident.test_ids == [T] and again.watch.repo == "o/r"


def test_unreproducible_failure_says_so(tmp_path):
    eng, posts = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    original = FakeGH.job_log
    FakeGH.job_log = lambda self, j: "npm ERR! build failed\n"
    try:
        eng.tick()
    finally:
        FakeGH.job_log = original
    assert "couldn't find a failing pytest test" in posts[-1] and eng.incident is None


def test_switching_repo_sets_aside_the_open_incident(tmp_path):
    eng, posts = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    assert eng.incident.status == "alerted"
    FakeGH.state["runs"] = []
    eng.start_watch("o/other", "main")
    assert eng.incident.status == "discarded"
    FakeGH.state["runs"] = [red_run(7)]
    eng.tick()
    assert eng.incident.status == "alerted" and eng.incident.run_number == 7


def test_fake_output_is_ignored(tmp_path):
    eng, _ = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    eng.fix_context()
    write_worktree(eng, BASE)
    term(eng, "python -m pytest -rA " + T, f"FAILED {T}\n", 1)
    write_worktree(eng, {"src/dates.py": b"x = 2\n"})
    # echoed text that only mentions pytest, and bare PASSED lines without a session, never count
    term(eng, f"echo 'PASSED {T}'  # pytest", f"PASSED {T}\n", 0)
    for _ in range(3):
        term(eng, "python -m pytest -rA " + T, f"PASSED {T}\n", 0, raw=True)
    assert eng.incident.ledger().passes_after(T) == 0
    assert not eng.proof()["accepted"]


def test_snapshot_for_desktop(tmp_path):
    eng, _ = make(tmp_path)
    assert eng.snapshot()["watch"] is None
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    s = eng.snapshot()
    assert s["watch"] == {"repo": "o/r", "branch": "main"}
    assert s["incident"]["status"] == "alerted" and s["incident"]["tests"] == [T] and s["incident"]["proof"] is None
    eng.fix_context()
    s = eng.snapshot()
    assert s["incident"]["proof"]["needed"] == 3 and s["incident"]["proof"]["accepted"] is False
    eng.discard()
    assert eng.snapshot()["incident"] is None
