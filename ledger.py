"""The proof ledger.

It records what Hermes's own terminal tool printed when the agent ran pytest,
and decides whether a fix has earned a PR. Nothing here runs a command: the
agent runs them through Hermes (and its approval prompt); the plugin only reads
the results in post_tool_call.

A fix is accepted only when, for every failing test id from CI:
  * at least one run before any source change shows it FAILED (or ERROR), and
  * the last three runs after the change all show it PASSED,
and, if a full-suite run was recorded before and after, no test that passed
before fails after.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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


@dataclass
class Ledger:
    test_ids: list[str]
    runs: list[Run] = field(default_factory=list)

    def record(self, output: str, exit_code: int | None, changed: bool, full_suite: bool = False) -> Run | None:
        results = parse_results(output)
        if not results:
            return None
        run = Run(changed=changed, results=results, exit_code=exit_code, full_suite=full_suite)
        self.runs.append(run)
        return run

    # ---- the gate -------------------------------------------------------------
    def fails_before(self, tid: str) -> int:
        return sum(1 for r in self.runs if not r.changed and r.results.get(tid) in ("FAILED", "ERROR"))

    def failed_before(self, tid: str) -> bool:
        return self.fails_before(tid) >= FAILS_NEEDED

    def flaky(self, tid: str) -> bool:
        """Passed at least once before any source change: the failure doesn't reproduce reliably."""
        return any(not r.changed and r.results.get(tid) == "PASSED" for r in self.runs)

    def passes_after(self, tid: str) -> int:
        """Consecutive PASSED results for tid, counted back from the latest changed run."""
        n = 0
        for r in reversed([r for r in self.runs if r.changed and tid in r.results]):
            if r.results[tid] != "PASSED":
                break
            n += 1
        return n

    def new_failures(self) -> list[str] | None:
        """Tests that passed in the last full-suite run before the change and fail in the last one after.

        None means no before/after pair of full-suite runs was recorded."""
        before = [r for r in self.runs if r.full_suite and not r.changed]
        after = [r for r in self.runs if r.full_suite and r.changed]
        if not before or not after:
            return None
        b, a = before[-1].results, after[-1].results
        return sorted(t for t, o in b.items() if o == "PASSED" and a.get(t) in ("FAILED", "ERROR"))

    def verdict(self) -> tuple[bool, list[str]]:
        """(accepted, reasons). Reasons explain every unmet condition."""
        why: list[str] = []
        for tid in self.test_ids:
            if self.flaky(tid):
                why.append(f"{tid}: passed before any change, so it's flaky, not broken")
            elif not self.failed_before(tid):
                why.append(f"{tid}: {self.fails_before(tid)}/{FAILS_NEEDED} failing runs before the change")
            p = self.passes_after(tid)
            if p < PASSES_NEEDED:
                why.append(f"{tid}: {p}/{PASSES_NEEDED} passing runs after the change")
        nf = self.new_failures()
        if nf:
            why.append("new failures in the full suite: " + ", ".join(nf))
        return (not why, why)

    def summary(self) -> dict:
        ok, why = self.verdict()
        nf = self.new_failures()
        return {
            "accepted": ok,
            "reasons": why,
            "tests": {t: {"failed_before": self.failed_before(t), "fails_before": self.fails_before(t), "flaky": self.flaky(t),
                          "passes_after": self.passes_after(t)} for t in self.test_ids},
            "suite": "not run" if nf is None else ("no new failures" if not nf else f"{len(nf)} new failures"),
        }
