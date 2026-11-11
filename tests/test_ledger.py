from crg import ledger

T = "tests/test_dates.py::test_parse_iso"
FAIL = f"FAILED {T} - AssertionError\n"
PASS = f"PASSED {T}\n"


def lg_with(*runs, suite=False):
    lg = ledger.Ledger([T])
    for out, changed in runs:
        lg.record(out, 0 if "PASSED" in out else 1, changed)
    if suite:  # a full-suite run before and after the change
        lg.record(f"FAILED {T}\nPASSED tests/a.py::other\n", 1, False, full_suite=True)
        lg.record(f"PASSED {T}\nPASSED tests/a.py::other\n", 0, True, full_suite=True)
    return lg


def test_accepts_fail_before_then_three_passes():
    ok, why = lg_with((FAIL, False), (FAIL, False), (PASS, True), (PASS, True), (PASS, True), suite=True).verdict()
    assert ok, why


def test_full_suite_is_required():
    ok, why = lg_with((FAIL, False), (FAIL, False), (PASS, True), (PASS, True), (PASS, True)).verdict()
    assert not ok and "full suite not run" in why[-1]


def test_one_failing_run_before_is_not_enough():
    ok, why = lg_with((FAIL, False), (PASS, True), (PASS, True), (PASS, True)).verdict()
    assert not ok and "1/2 failing runs before" in why[0]


def test_a_pass_before_the_change_means_flaky():
    lg = lg_with((FAIL, False), (PASS, False), (FAIL, False), (PASS, True), (PASS, True), (PASS, True))
    ok, why = lg.verdict()
    assert lg.flaky(T) and not ok and "flaky" in why[0]
    assert lg.summary()["tests"][T]["flaky"]


def test_refuses_without_seeing_the_failure():
    ok, why = lg_with((PASS, True), (PASS, True), (PASS, True)).verdict()
    assert not ok
    assert "0/2 failing runs before" in why[0]


def test_refuses_with_two_passes():
    ok, why = lg_with((FAIL, False), (FAIL, False), (PASS, True), (PASS, True)).verdict()
    assert not ok
    assert "2/3" in why[0]


def test_a_failure_after_the_change_resets_the_count():
    lg = lg_with((FAIL, False), (PASS, True), (PASS, True), (FAIL, True), (PASS, True), (PASS, True))
    assert lg.passes_after(T) == 2
    assert not lg.verdict()[0]


def test_failure_after_change_does_not_count_as_before():
    lg = lg_with((FAIL, True), (PASS, True), (PASS, True), (PASS, True))
    assert not lg.failed_before(T)


def test_exit_code_alone_is_not_enough():
    # exit 0 with no PASSED line (e.g. the test was deselected or skipped) proves nothing
    lg = ledger.Ledger([T])
    assert lg.record("no tests ran in 0.01s\n", 0, True) is None
    lg.record(f"SKIPPED {T}\n", 0, True)
    assert lg.passes_after(T) == 0


def test_regression_gate():
    lg = ledger.Ledger([T])
    lg.record(f"FAILED {T}\nPASSED tests/a.py::other\n", 1, False, full_suite=True)
    lg.record(f"PASSED {T}\nFAILED tests/a.py::other\n", 1, True, full_suite=True)
    assert lg.new_failures() == ["tests/a.py::other"]
    for _ in range(3):
        lg.record(PASS, 0, True)
    ok, why = lg.verdict()
    assert not ok and "new failures" in why[-1]


def test_suite_not_run_is_reported():
    assert lg_with((FAIL, False)).summary()["suite"] == "not run"


def test_several_sessions_in_one_output_count_separately():
    one = "============================= test session starts ==============================\n" + PASS + "1 passed\n"
    chunks = ledger.split_sessions("$ for i in 1 2 3\n" + one * 3)
    assert len(chunks) == 3
    lg = ledger.Ledger([T])
    lg.record(FAIL, 1, False)
    lg.record(FAIL, 1, False)
    for c in chunks:
        lg.record(c, None, True)
    lg.record(f"FAILED {T}\nPASSED tests/a.py::other\n", 1, False, full_suite=True)
    lg.record(f"PASSED {T}\nPASSED tests/a.py::other\n", 0, True, full_suite=True)
    assert lg.passes_after(T) == 4 and lg.verdict()[0]


def test_results_with_spaced_params():
    out = (
        "PASSED tests/test_filesize.py::test_naturalsize[test_args31-1.0 MB]\n"
        "FAILED tests/test_filesize.py::test_naturalsize[test_args32-1.0 GB] - AssertionError: x\n"
    )
    assert ledger.parse_results(out) == {
        "tests/test_filesize.py::test_naturalsize[test_args31-1.0 MB]": "PASSED",
        "tests/test_filesize.py::test_naturalsize[test_args32-1.0 GB]": "FAILED",
    }
