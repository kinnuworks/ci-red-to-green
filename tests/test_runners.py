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
    assert lg.verdict() == (True, [])


def test_pytest_logs_still_pick_pytest():
    log = "2026-11-11T10:00:00.0Z FAILED tests/test_a.py::test_b - AssertionError: x\n"
    f = runners.parse_any(log)
    assert f.runner == "pytest" and f.test_ids == ["tests/test_a.py::test_b"]
