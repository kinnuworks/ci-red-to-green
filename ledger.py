"""The proof ledger.

It records what Hermes's own terminal tool printed when the agent ran pytest,
and decides whether a fix has earned a PR. Nothing here runs a command: the
agent runs them through Hermes (and its approval prompt); the plugin only reads
the results in post_tool_call.

A fix is accepted only when, for every failing test id from CI:
  * two runs before any source change show it FAILED (or ERROR), and none PASSED,
  * when there was a green run before: one run with the changed source files put back
    as they were at that green commit shows it PASSED,
  * the last three runs after the change, on the exact code that ships, show it PASSED,
and a full-suite run before and after the change shows no test that passed before
now failing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .logparse import TEST_ID

# pytest -rA short summary lines: "PASSED tests/x.py::test_y", "FAILED tests/x.py::test_y - msg"
_RESULT = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (" + TEST_ID + r")(?: - .*)?$")
PASSES_NEEDED = 3
# Reproduced twice before any change; a test that passes before the change is flaky, not broken.
FAILS_NEEDED = 2


_SESSION = re.compile(r"^=+ test session starts =+$", re.M)


def split_sessions(output: str) -> list[str]:
    """One terminal call can hold several pytest sessions (a loop, `a && b`). Split them so each counts once."""
    starts = [m.start() for m in _SESSION.finditer(output)]
    if len(starts) <= 1:
        return [output]
    return [output[a:b] for a, b in zip(starts, starts[1:] + [len(output)], strict=True)]


_SUMMARY = re.compile(r"^=+ .*\b(?:passed|failed|error|errors|skipped|deselected|no tests ran)\b.* in [0-9.]+s(?: \([0-9:]+\))? =+$", re.M)


def is_real_session(chunk: str) -> bool:
    """A pytest run prints its session header and a closing summary line with a duration.
    Text echoed by hand rarely has both; anything that doesn't is ignored."""
    return bool(_SESSION.search(chunk) and _SUMMARY.search(chunk))


def parse_results(output: str) -> dict[str, str]:
    """Map test id -> outcome from pytest -rA output."""
    res: dict[str, str] = {}
    for line in output.splitlines():
        m = _RESULT.match(line.strip())
        if m:
            res[m.group(2)] = m.group(1)
    return res


@dataclass
class Run:
    changed: bool  # was the worktree's source already changed when this ran?
    results: dict[str, str]
    exit_code: int | None
    full_suite: bool
    anchor: bool = False  # ran with the source files restored to the last green commit
    state: str = ""  # hash of the source changes the run saw; the proof must match the code that ships
    cmd: str = ""  # the test command as run, with local paths removed, so a reviewer can rerun it


@dataclass
class Ledger:
    test_ids: list[str]
    runs: list[Run] = field(default_factory=list)
    parse: Callable[[str], dict[str, str]] | None = field(default=None, repr=False, compare=False)

    def record(self, output: str, exit_code: int | None, changed: bool, full_suite: bool = False,
               anchor: bool = False, state: str = "") -> Run | None:
        results = (self.parse or parse_results)(output)
        if not results:
            return None
        run = Run(changed=changed, results=results, exit_code=exit_code, full_suite=full_suite, anchor=anchor, state=state)
        self.runs.append(run)
        return run

    # ---- the gate -------------------------------------------------------------
    def _main(self) -> list[Run]:
        return [r for r in self.runs if not r.anchor]

    def at_green(self, tid: str) -> str | None:
        """The test's latest result with the source restored to the last green commit, if it was run that way."""
        res = [r.results[tid] for r in self.runs if r.anchor and tid in r.results]
        return res[-1] if res else None

    def fails_before(self, tid: str) -> int:
        return sum(1 for r in self._main() if not r.changed and r.results.get(tid) in ("FAILED", "ERROR"))

    def failed_before(self, tid: str) -> bool:
        return self.fails_before(tid) >= FAILS_NEEDED

    def flaky(self, tid: str) -> bool:
        """Passed at least once before any source change: the failure doesn't reproduce reliably."""
        return any(not r.changed and r.results.get(tid) == "PASSED" for r in self._main())

    def passes_after(self, tid: str, state: str | None = None) -> int:
        """Consecutive PASSED results for tid, counted back from the latest changed run. With state,
        only runs of exactly that source state count: an edit after the passes starts the count over."""
        n = 0
        for r in reversed([r for r in self._main() if r.changed and tid in r.results]):
            if r.results[tid] != "PASSED" or (state is not None and r.state != state):
                break
            n += 1
        return n

    def new_failures(self, state: str | None = None) -> list[str] | None:
        """Tests that passed in the last full-suite run before the change and don't pass in the last
        one after: failed, errored or skipped.

        None means no before/after pair of full-suite runs was recorded (for this source state)."""
        before = [r for r in self._main() if r.full_suite and not r.changed]
        after = [r for r in self._main() if r.full_suite and r.changed and (state is None or r.state == state)]
        if not before or not after:
            return None
        b, a = before[-1].results, after[-1].results
        # A test missing from the after run is not counted: Hermes may cut long terminal output,
        # so a full-suite run can record only part of the per-test lines.
        return sorted(t for t, o in b.items() if o == "PASSED" and t not in self.test_ids
                      and a.get(t) not in ("PASSED", None))

    def verdict(self, state: str | None = None, need_green: bool = False) -> tuple[bool, list[str]]:
        """(accepted, reasons). Reasons explain every unmet condition. need_green: a last green
        commit exists, so the regression must be confirmed there too."""
        why: list[str] = []
        for tid in self.test_ids:
            if need_green:
                g = self.at_green(tid)
                if g is None:
                    why.append(f"{tid}: not yet run with the source as it was at the last green commit")
                elif g != "PASSED":
                    why.append(f"{tid}: also fails at the last green commit, so these commits didn't cause it")
            if self.flaky(tid):
                why.append(f"{tid}: passed before any change, so it's flaky, not broken")
            elif not self.failed_before(tid):
                why.append(f"{tid}: {self.fails_before(tid)}/{FAILS_NEEDED} failing runs before the change")
            p = self.passes_after(tid, state)
            if p < PASSES_NEEDED:
                why.append(f"{tid}: {p}/{PASSES_NEEDED} passing runs after the change")
        nf = self.new_failures(state)
        if nf is None:
            why.append("full suite not run both before and after the change")
        elif nf:
            why.append("new failures in the full suite: " + ", ".join(nf))
        return (not why, why)

    def summary(self, state: str | None = None, need_green: bool = False) -> dict:
        ok, why = self.verdict(state, need_green)
        nf = self.new_failures(state)
        return {
            "accepted": ok,
            "reasons": why,
            "tests": {t: {"failed_before": self.failed_before(t), "fails_before": self.fails_before(t), "flaky": self.flaky(t), "at_green": self.at_green(t),
                          "passes_after": self.passes_after(t, state)} for t in self.test_ids},
            "suite": "not run" if nf is None else ("no new failures" if not nf else f"{len(nf)} new failures"),
        }
