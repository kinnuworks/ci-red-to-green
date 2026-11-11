import json
from pathlib import Path

import pytest

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
        runs = self.state["runs"]
        return runs.get(self.repo, []) if isinstance(runs, dict) else runs

    def failed_jobs(self, run_id):
        return [{"id": 7}]

    def job_log(self, job_id):
        return LOG

    def tree(self, sha):
        files = self.state.get("trees", {}).get(sha, BASE)
        return {p: E.git_blob_sha(b) for p, b in files.items()}

    def blob(self, sha):
        every = [BASE] + list(self.state.get("trees", {}).values())
        return {E.git_blob_sha(b): b for files in every for b in files.values()}[sha]

    def compare_files(self, base, head):
        return self.state.get("files", [])

    def compare(self, base, head):
        self.state["compared"] = (base, head)
        return self.state.get("commits", [])

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
    assert "couldn't find a failing pytest or go test" in posts[-1] and eng.incident is None


def test_several_repos_queue_behind_the_open_incident(tmp_path):
    eng, posts = make(tmp_path)
    FakeGH.state["runs"] = {}
    eng.start_watch("o/r", "main")
    assert "Also watching" in eng.start_watch("o/other", "dev")
    FakeGH.state["runs"] = {"o/r": [red_run(5)], "o/other": [red_run(9)]}
    eng.tick()
    assert eng.incident.repo == "o/r" and eng.incident.run_number == 5
    assert "Also red: o/other @ dev (run #9). Queued" in posts[-1]
    assert eng.snapshot()["queue"][0]["repo"] == "o/other"
    eng.tick()  # nothing new: the queued run isn't posted twice
    assert sum("Also red" in p for p in posts) == 1
    assert "Next in the queue: o/other" in eng.discard()
    eng.tick()
    assert eng.incident.repo == "o/other" and eng.incident.branch == "dev" and eng.incident.status == "alerted"
    assert "CI red on dev of o/other" in posts[-1]
    assert eng.watch.repo == "o/other"
    assert "o/other" in eng.fix_context() and eng.queue == []


def test_repointing_a_repo_to_another_branch_sets_aside_its_incident(tmp_path):
    eng, posts = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    FakeGH.state["runs"] = []
    eng.start_watch("o/r", "release")
    assert eng.incident.status == "discarded"
    assert [w.branch for w in eng.watches] == ["release"]
    assert "Stopped watching o/r" in eng.unwatch("o/r") and eng.watches == []


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


@pytest.mark.parametrize(
    "cmd,runs",
    [
        ("git clone https://github.com/o/r /w", False),
        ("cd /w && git status && cat datekit/parse.py | head -40", False),
        ("cd /w && python -m pytest -rA", True),
        ("cd /w && pip install -e .", True),
        ("cd /w && make test", True),
        ("cd /w && ls && python setup.py develop", True),
        ("test -d /w && echo exists || (git clone https://github.com/o/r /w && git -C /w checkout --detach abc)", False),
        ("[ -d /w ] || mkdir -p /w", False),
        ("cd /w && ls -la && find . -maxdepth 3 -not -path './.venv*' | head -50", False),
        ("sed -n 1,40p /w/a.py", False),
        ("find /w -name '*.py' -exec python {} \\;", True),
        ("find /w -delete", True),
        ("sed -i s/a/b/ /w/a.py", True),
    ],
)
def test_runs_repo_code(cmd, runs):
    assert E.runs_repo_code(cmd) is runs


def test_install_step_is_escalated_before_pytest(tmp_path):
    eng, _posts = make(tmp_path)
    FakeGH.state["runs"] = []
    eng.start_watch("o/r", "main")
    eng.tick()
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    assert eng.wants_fix("fix")
    eng.fix_context()
    wt = eng.incident.worktree
    assert eng.needs_approval("terminal", {"command": f"git clone https://github.com/o/r {wt}"}) is None
    assert eng.needs_approval("terminal", {"command": f"cd {wt} && pip install -e ."}) == "approve"


def test_snapshot_skips_gitignored_untracked_files(tmp_path):
    root = tmp_path / "w"
    (root / "pkg").mkdir(parents=True)
    (root / ".gitignore").write_text("*.log\n/out/\ncoverage.xml\n")
    (root / "pkg" / "mod.py").write_text("x = 1\n")
    (root / "pkg" / "new.py").write_text("y = 2\n")
    (root / "pkg" / "debug.log").write_text("noise\n")
    (root / "out").mkdir()
    (root / "out" / "a.txt").write_text("gen\n")
    (root / "coverage.xml").write_text("<x/>\n")
    (root / "kept.log").write_text("tracked\n")
    snap = E.blob_snapshot(str(root), {"kept.log": "abc"})
    assert set(snap) == {".gitignore", "pkg/mod.py", "pkg/new.py", "kept.log"}


def green_run(n, sha):
    return {"id": 100 + n, "run_number": n, "status": "completed", "conclusion": "success", "head_sha": sha, "html_url": ""}


def test_alert_names_the_commit_since_the_last_green_run(tmp_path):
    eng, posts = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run(12), green_run(11, "b" * 40)]
    FakeGH.state["commits"] = [{"sha": "a" * 40, "commit": {"message": "Simplify month parsing\n\nbody"}, "author": {"login": "dev1"}}]
    eng.tick()
    assert FakeGH.state["compared"] == ("b" * 40, "a" * 40)
    assert 'Since the last green run: aaaaaaa "Simplify month parsing" by dev1' in posts[-1]
    assert eng.wants_fix("fix")
    ctx = eng.fix_context()
    assert "The last green commit was bbbbbbb" in ctx and "aaaaaaa Simplify month parsing" in ctx
    assert eng.snapshot()["incident"]["suspects"][0]["author"] == "dev1"


def test_card_shows_the_diff_and_flaky_tests_are_refused(tmp_path):
    eng, posts = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    eng.fix_context()
    write_worktree(eng, BASE)
    term(eng, "python -m pytest -rA " + T, f"FAILED {T}\n", 1)
    term(eng, "python -m pytest -rA " + T, f"PASSED {T}\n", 0)  # passes with nothing changed: flaky
    write_worktree(eng, {"src/dates.py": b"def month(s):\n    return int(s[5:7])\n"})
    for _ in range(3):
        term(eng, "python -m pytest -rA " + T, f"PASSED {T}\n", 0)
    card = eng.card_text()
    assert "flaky: passed before any change" in card and "Not ready" in card
    assert "-    return int(s[5:7]) - 1" in card and "+    return int(s[5:7])" in card
    assert eng.open_pr().startswith("Not opening a PR")


def test_demo_runs_the_whole_loop_without_github(tmp_path):
    from crg import demo

    posts = []
    eng = E.Engine(tmp_path / "data", token_fn=lambda: None, post=posts.append)
    msg = eng.start_demo()
    assert "PR #329" in msg
    inc = eng.incident
    assert inc and inc.status == "alerted" and len(inc.test_ids) == 4
    assert "CI red on main: tests/test_filesize.py::test_naturalsize[test_args12-1.0 MB]" in posts[-1]
    assert "'1000.0 kB' == '1.0 MB'" in posts[-1]
    assert eng.wants_fix("fix")
    ctx = eng.fix_context()
    assert "Nothing to install beyond pytest" in ctx
    ids = " ".join(f"FAILED {t}\n" for t in inc.test_ids)
    passed = "".join(f"PASSED {t}\n" for t in inc.test_ids)
    for _ in range(2):
        term(eng, inc.command, ids.replace(" FAILED", "FAILED"), 1)
    fixed = Path(inc.worktree, "humanize_mini/filesize.py")
    src = fixed.read_text().replace(
        "    space = \"\" if gnu else \" \"\n",
        "    if exp < len(suffix) and abs(float(format % (abs_bytes / (base**exp)))) >= base:\n        exp += 1\n    space = \"\" if gnu else \" \"\n",
    )
    fixed.write_text(src)
    for _ in range(3):
        term(eng, inc.command, passed, 0)
    assert eng.incident.status == "ready", eng.proof()["reasons"]
    out = eng.open_pr()
    assert "patch" in out and eng.incident.status == "done"
    patch = Path(eng.incident.pr_url).read_text()
    assert "+++ b/humanize_mini/filesize.py" in patch and "+        exp += 1" in patch
    assert "tests/" not in patch
    assert demo.SAMPLE.joinpath("humanize_mini/filesize.py").read_text().count("exp += 1") == 0  # sample untouched


def test_snapshot_handles_nested_gitignore_and_negation(tmp_path):
    root = tmp_path / "w"
    (root / "pkg" / "gen").mkdir(parents=True)
    (root / ".gitignore").write_text("*.log\n!keep.log\nbuild/\n")
    (root / "pkg" / ".gitignore").write_text("gen/\n*.tmp\n")
    (root / "pkg" / "mod.py").write_text("x = 1\n")
    (root / "pkg" / "gen" / "out.py").write_text("generated\n")
    (root / "pkg" / "scratch.tmp").write_text("t\n")
    (root / "keep.log").write_text("k\n")
    (root / "noise.log").write_text("n\n")
    (root / "docs").mkdir()
    (root / "docs" / "build").mkdir()
    (root / "docs" / "build" / "index.html").write_text("<html/>\n")
    (root / "top.tmp").write_text("not ignored at the root\n")
    snap = E.blob_snapshot(str(root))
    assert set(snap) == {".gitignore", "pkg/.gitignore", "pkg/mod.py", "keep.log", "top.tmp"}


def test_a_go_incident_uses_go_test_end_to_end(tmp_path, monkeypatch):
    go_log = (Path(__file__).parent / "data" / "go-test.txt").read_text()
    monkeypatch.setattr(FakeGH, "job_log", lambda self, job_id: go_log)
    eng, posts = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    inc = eng.incident
    assert inc.runner == "go" and inc.test_ids == ["example.com/demo/size::TestRollover"]
    assert "go test -v -count=1 -run '^(TestRollover)$' example.com/demo/size" in eng.fix_context()
    assert "go test -v -count=1 ./..." in eng.fix_context()
    write_worktree(eng, BASE)
    red = (Path(__file__).parent / "data" / "go-test-v.txt").read_text()
    green = "=== RUN   TestRollover\n--- PASS: TestRollover (0.00s)\nPASS\nok  \texample.com/demo/size\t0.2s\n"
    cmd = f"cd {inc.worktree} && {inc.command}"
    for out, code in [(red, 1), (red, 1)]:
        eng.observe("terminal", {"command": cmd}, json.dumps({"output": out, "exit_code": code}))
    write_worktree(eng, {"src/dates.py": b"def month(s):\n    return int(s[5:7])\n"})
    for _ in range(3):
        eng.observe("terminal", {"command": cmd}, json.dumps({"output": green, "exit_code": 0}))
    eng.observe("terminal", {"command": "echo go test"}, json.dumps({"output": green, "exit_code": 0}))
    p = eng.proof()
    assert p["tests"]["example.com/demo/size::TestRollover"]["passes_after"] == 3
    assert "(go)" in eng.pr_body()


def test_runs_with_edited_tests_never_count_and_pr_waits_for_every_workflow(tmp_path):
    eng, posts = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    eng.fix_context()
    write_worktree(eng, BASE)
    for _ in range(2):
        term(eng, "python -m pytest -rA " + T, f"FAILED {T}\n", 1)
    write_worktree(eng, {"src/dates.py": b"def month(s):\n    return int(s[5:7])\n", "tests/test_dates.py": b"def test_parse_iso(): pass\n"})
    for _ in range(3):
        term(eng, "python -m pytest -rA " + T, f"PASSED {T}\n", 0)
    write_worktree(eng, {"tests/test_dates.py": BASE["tests/test_dates.py"]})  # edit undone before /ci
    p = eng.proof()
    assert p["tests"][T]["passes_after"] == 0 and not p["accepted"]
    assert p["ignored_runs"] == 3 and "were not counted" in eng.card_text()
    for _ in range(3):
        term(eng, "python -m pytest -rA " + T, f"PASSED {T}\n", 0)
    assert eng.incident.status == "ready"
    assert eng.open_pr().startswith("PR opened"), eng.proof()["reasons"]
    FakeGH.state["pr_runs"] = [{"status": "completed", "conclusion": "success", "html_url": "u1"}, {"status": "in_progress"}]
    eng.tick()
    assert eng.incident.status == "pr_open"  # one workflow still running
    FakeGH.state["pr_runs"][1] = {"status": "completed", "conclusion": "success", "html_url": "u2"}
    eng.tick()
    assert eng.incident.status == "done"


def test_worktree_match_respects_path_boundaries(tmp_path):
    eng, _ = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    wt = eng.incident.worktree
    assert eng._in_worktree(f"cd {wt} && pytest", None)
    assert not eng._in_worktree(f"cd {wt}0 && pytest", None)
    assert not eng._in_worktree("pytest", wt + "0")


def test_three_point_proof_green_red_green(tmp_path):
    green_src = b"def month(s):\n    return int(s[5:7])\n"
    eng, posts = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run(12), green_run(11, "b" * 40)]
    FakeGH.state["commits"] = [{"sha": "a" * 40, "commit": {"message": "Off by one"}, "author": {"login": "dev1"}}]
    FakeGH.state["files"] = ["src/dates.py", "tests/test_dates.py"]
    FakeGH.state["trees"] = {"b" * 40: {**BASE, "src/dates.py": green_src}}
    eng.tick()
    inc = eng.incident
    assert inc.anchor_blobs == {"src/dates.py": E.git_blob_sha(green_src)}  # tests are never part of the anchor
    ctx = eng.fix_context()
    assert f"checkout {'b' * 40} -- src/dates.py" in ctx and f"checkout {'a' * 40} -- src/dates.py" in ctx
    write_worktree(eng, BASE)
    for _ in range(2):
        term(eng, "python -m pytest -rA " + T, f"FAILED {T}\n", 1)
    write_worktree(eng, {"src/dates.py": green_src})  # restored to green: the anchor run
    term(eng, "python -m pytest -rA " + T, f"PASSED {T}\n", 0)
    write_worktree(eng, BASE)  # back to red
    assert eng.proof()["tests"][T]["at_green"] == "PASSED" and eng.proof()["tests"][T]["passes_after"] == 0
    write_worktree(eng, {"src/dates.py": green_src})  # the fix happens to restore the green code
    for _ in range(3):
        term(eng, "python -m pytest -rA " + T, f"PASSED {T}\n", 0)
    p = eng.proof()
    assert p["tests"][T]["passes_after"] == 3 and p["accepted"], p["reasons"]
    assert "PASSED at last green bbbbbbb" in eng.card_text()
    assert "at the last green commit (bbbbbbb)" in eng.pr_body()


def test_read_only_token_gets_the_proven_patch(tmp_path, monkeypatch):
    def denied(self, *a, **k):
        raise E.GitHubError(403, "Resource not accessible by personal access token")
    monkeypatch.setattr(FakeGH, "create_branch_commit", denied)
    eng, posts = make(tmp_path)
    eng.start_watch("o/r", "main")
    FakeGH.state["runs"] = [red_run()]
    eng.tick()
    eng.fix_context()
    write_worktree(eng, BASE)
    for _ in range(2):
        term(eng, "python -m pytest -rA " + T, f"FAILED {T}\n", 1)
    write_worktree(eng, {"src/dates.py": b"def month(s):\n    return int(s[5:7])\n"})
    for _ in range(3):
        term(eng, "python -m pytest -rA " + T, f"PASSED {T}\n", 0)
    out = eng.open_pr()
    assert "can't push" in out and "git apply" in out and eng.incident.status == "done"
    patch = Path(eng.incident.pr_url).read_text()
    assert "+++ b/src/dates.py" in patch and "FAILED 2/2" in patch and "tests/" not in patch.split("+++")[1]
