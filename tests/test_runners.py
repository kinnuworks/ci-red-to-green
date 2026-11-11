"""Runner parsing, checked against real `go test` output (tests/data, captured from go 1.27)."""

from pathlib import Path

from crg import ledger, runners

DATA = Path(__file__).parent / "data"
GO = runners.get("go")


def actions(text):
    return "".join(f"2026-11-11T10:00:{i % 60:02d}.0000000Z {ln}\n" for i, ln in enumerate(text.splitlines()))


def test_go_failures_from_an_actions_log_without_v():
    f = runners.parse_any(actions((DATA / "go-test.txt").read_text()))
    assert f.runner == "go"
    assert f.test_ids == ["example.com/demo/size::TestRollover"]
    assert f.reasons[f.test_ids[0]] == 'Human(999999) = "1000.0 kB", want "1.0 MB"'


def test_go_failures_from_a_verbose_log():
    f = GO.parse_log((DATA / "go-test-v.txt").read_text())
    assert f.test_ids == ["example.com/demo/size::TestRollover"]
    assert "want \"1.0 MB\"" in f.reasons[f.test_ids[0]]


def test_go_command_reruns_only_the_failures():
    cmd = GO.command(["example.com/demo/size::TestRollover", "example.com/demo/util::TestAdd"])
    assert cmd == "go test -v -count=1 -run '^(TestAdd|TestRollover)$' example.com/demo/size example.com/demo/util"
    assert GO.is_cmd("cd /w && " + cmd) and not GO.is_cmd("echo go test")


def test_go_results_and_real_sessions():
    out = (DATA / "go-test-v.txt").read_text()
    assert GO.parse_results(out) == {
        "example.com/demo/size::TestHuman": "PASSED",
        "example.com/demo/size::TestRollover": "FAILED",
        "example.com/demo/util::TestAdd": "PASSED",
    }
    assert GO.is_real_session(out)
    assert not GO.is_real_session("--- PASS: TestRollover (0.00s)\nok  \texample.com/demo/size\t(cached)\n")
    assert not GO.is_real_session("--- PASS: TestRollover (0.00s)\n")


def test_go_ledger_gate():
    t = "example.com/demo/size::TestRollover"
    red = (DATA / "go-test-v.txt").read_text()
    green = "=== RUN   TestRollover\n--- PASS: TestRollover (0.00s)\nPASS\nok  \texample.com/demo/size\t0.2s\n"
    lg = ledger.Ledger([t], parse=GO.parse_results)
    for out, changed in [(red, False), (red, False), (green, True), (green, True), (green, True)]:
        lg.record(out, None, changed)
    lg.record(red, None, False, full_suite=True)
    lg.record(green.replace("ok  ", "ok  ") + "--- PASS: TestHuman (0.00s)\nok  \texample.com/demo/size\t0.1s\n=== RUN   TestAdd\n--- PASS: TestAdd (0.00s)\nok  \texample.com/demo/util\t0.1s\n", None, True, full_suite=True)
    assert lg.verdict() == (True, [])


def test_pytest_logs_still_pick_pytest():
    log = "2026-11-11T10:00:00.0Z FAILED tests/test_a.py::test_b - AssertionError: x\n"
    f = runners.parse_any(log)
    assert f.runner == "pytest" and f.test_ids == ["tests/test_a.py::test_b"]


def test_only_a_plain_test_run_counts():
    py = runners.get("pytest")
    ok = ["cd /w && python -m pytest -rA 'tests/a.py::t[1.0 MB]'", ".venv/bin/python -m pytest -rA # full suite",
          "cd /w && source .venv/bin/activate && pytest -rA", "PYTHONPATH=src python -m pytest -rA"]
    bad = ["python -m pytest -rA; echo 'PASSED tests/a.py::t'", "python -m pytest -rA | tail -5",
           "echo hi && python -m pytest -rA", "python -m pytest -rA > out.txt", "python -m pytest -rA && cat fake.txt",
           "python -c 'print(1)' && pytest", "for i in 1 2 3; do pytest; done"]
    assert all(py.is_cmd(c) for c in ok), [c for c in ok if not py.is_cmd(c)]
    assert not any(py.is_cmd(c) for c in bad), [c for c in bad if py.is_cmd(c)]


def test_results_only_count_inside_the_pytest_session():
    py = runners.get("pytest")
    out = ("PASSED tests/a.py::t\n"
           "============================= test session starts ==============================\n"
           "FAILED tests/a.py::t - AssertionError\n"
           "========================= 1 failed in 0.02s =========================\n"
           "PASSED tests/a.py::t\n")
    assert py.parse_results(out) == {"tests/a.py::t": "FAILED"}


def test_full_suite_is_judged_by_the_command():
    py = runners.get("pytest")
    assert py.is_full("cd /w && python -m pytest -rA")
    assert py.is_full("cd /w && source .venv/bin/activate && RUN=after python -m pytest -rA -p no:cacheprovider")
    assert not py.is_full("python -m pytest -rA tests/test_a.py")
    assert not py.is_full("python -m pytest -rA 'tests/a.py::t[1]'")
    assert not py.is_full("python -m pytest -rA -k parse")
    assert not py.is_full("python -m pytest -rA tests/")
    assert runners.get("go").is_full("go test -v -count=1 ./...")
    assert not runners.get("go").is_full("go test -v -count=1 -run '^(TestA)$' ./...")


def test_wrapped_pytest_commands_count():
    py = runners.get("pytest")
    for c in ("uv run pytest -rA", "cd /w && uv run --frozen pytest -rA tests/a.py::t", "poetry run pytest -rA", "hatch run python -m pytest -rA"):
        assert py.is_cmd(c), c
    assert not py.is_cmd("uv run python -c 'print(1)'")
