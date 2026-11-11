from crg import logparse

LOG = """\
2026-11-11T06:01:02.1234567Z ##[group]Run python -m pytest -rA
2026-11-11T06:01:05.0000000Z ============================= test session starts ==============================
2026-11-11T06:01:05.1000000Z tests/test_dates.py::test_parse_iso FAILED                              [ 50%]
2026-11-11T06:01:05.2000000Z \x1b[31mFAILED\x1b[0m tests/test_dates.py::test_parse_iso - AssertionError: assert 11 == 12
2026-11-11T06:01:05.2000000Z FAILED tests/test_money.py::test_round[half-up] - ValueError: bad
2026-11-11T06:01:05.3000000Z ========================= 2 failed, 14 passed in 0.31s =========================
2026-11-11T06:01:05.4000000Z ##[error]Process completed with exit code 1.
"""


def test_finds_failed_ids_and_reasons():
    f = logparse.parse_log(LOG)
    assert f.test_ids == ["tests/test_dates.py::test_parse_iso", "tests/test_money.py::test_round[half-up]"]
    assert f.reasons["tests/test_dates.py::test_parse_iso"] == "AssertionError: assert 11 == 12"
    assert "2 failed" in f.summary


def test_command_reproduces_only_the_failures():
    f = logparse.parse_log(LOG)
    assert f.command == "python -m pytest -rA tests/test_dates.py::test_parse_iso tests/test_money.py::test_round[half-up]"


def test_no_pytest_failure_is_falsy():
    f = logparse.parse_log("2026-11-11T06:01:02Z npm ERR! build failed\n")
    assert not f
    assert f.test_ids == []


def test_duplicates_collapse():
    f = logparse.parse_log("FAILED tests/a.py::t - x\nFAILED tests/a.py::t - x\n")
    assert f.test_ids == ["tests/a.py::t"]


def test_error_lines_count():
    f = logparse.parse_log("ERROR tests/a.py::t - fixture missing\n")
    assert f.test_ids == ["tests/a.py::t"]
