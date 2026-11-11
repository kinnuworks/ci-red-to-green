"""The test runners ci-red-to-green understands: pytest and `go test`.

Each runner knows how to read a red GitHub Actions log, how to rerun exactly the failing
tests, how to recognise a real run of itself in Hermes's terminal output, and how to read
per-test results from that output. Pure functions, no network.
"""

from __future__ import annotations

import re
import shlex

from . import ledger, logparse


class Pytest:
    name = "pytest"
    full_command = "python -m pytest -rA"
    install_hint = "for example python -m pip install -e . pytest"
    flags_note = "Always pass -rA to pytest"
    # The command must actually invoke pytest (optionally after cd/&&), not just mention it.
    _CMD = re.compile(r"(?:^|&&|;|\|\|)\s*(?:\S*python\S*\s+-m\s+pytest|\S*/?pytest)(?:\s|$)")

    def parse_log(self, text: str) -> logparse.Failure:
        return logparse.parse_log(text)

    def command(self, ids: list[str]) -> str:
        return f"python -m pytest -rA {' '.join(shlex.quote(t) for t in ids)}".strip()

    def is_cmd(self, cmd: str) -> bool:
        return bool(self._CMD.search(cmd or ""))

    def split_sessions(self, output: str) -> list[str]:
        return ledger.split_sessions(output)

    def is_real_session(self, chunk: str) -> bool:
        return ledger.is_real_session(chunk)

    def parse_results(self, output: str) -> dict[str, str]:
        return ledger.parse_results(output)


# go test -v: "=== RUN   TestX", "--- FAIL: TestX (0.00s)", "    x_test.go:12: message",
# then one line per package: "FAIL\texample.com/m/pkg\t0.51s" or "ok  \texample.com/m/pkg\t0.12s".
_GO_RUN = re.compile(r"^=== RUN\s+(Test\w*)$")
_GO_RES = re.compile(r"^--- (PASS|FAIL|SKIP): (Test\w*) \(")
_GO_MSG = re.compile(r"^\s+\S+_test\.go:\d+: (.*)$")
_GO_PKG = re.compile(r"^(ok|FAIL)\s+(\S+)\s+(?:[0-9.]+s|\(cached\))")
_GO_PKG_TIMED = re.compile(r"^(ok|FAIL)\s+\S+\s+[0-9.]+s", re.M)


def _go_scan(text: str) -> list[tuple[str, str, str]]:
    """(package, test, outcome) for every top-level test, plus the first message per test."""
    out: list[tuple[str, str, str]] = []
    pending: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = logparse.clean(raw)
        m = _GO_RES.match(line)
        if m:
            pending.append((m.group(2), {"PASS": "PASSED", "FAIL": "FAILED", "SKIP": "SKIPPED"}[m.group(1)]))
            continue
        m = _GO_PKG.match(line)
        if m:
            out.extend((m.group(2), t, o) for t, o in pending)
            pending = []
    return out


class GoTest:
    name = "go"
    full_command = "go test -v -count=1 ./..."
    install_hint = "Go modules download on the first run"
    flags_note = "Always pass -v -count=1 to go test"
    _CMD = re.compile(r"(?:^|&&|;|\|\|)\s*go\s+test(?:\s|$)")

    def parse_log(self, text: str) -> logparse.Failure:
        f = logparse.Failure()
        reasons: dict[str, str] = {}
        current = ""
        for raw in text.splitlines():
            line = logparse.clean(raw)
            m = _GO_RUN.match(line) or _GO_RES.match(line)
            if m:
                current = m.group(m.lastindex)
                continue
            m = _GO_MSG.match(line)
            if m and current and current not in reasons:
                reasons[current] = m.group(1).strip()
        for pkg, test, outcome in _go_scan(text):
            tid = f"{pkg}::{test}"
            if outcome == "FAILED" and tid not in f.test_ids:
                f.test_ids.append(tid)
                f.reasons[tid] = reasons.get(test, "")
        f.runner = self.name
        return f

    def command(self, ids: list[str]) -> str:
        pkgs = sorted({t.split("::")[0] for t in ids})
        tests = sorted({t.split("::")[-1] for t in ids})
        return f"go test -v -count=1 -run {shlex.quote('^(' + '|'.join(tests) + ')$')} {' '.join(pkgs)}"

    def is_cmd(self, cmd: str) -> bool:
        return bool(self._CMD.search(cmd or ""))

    def split_sessions(self, output: str) -> list[str]:
        return [output]  # one go test call reports every package it ran

    def is_real_session(self, chunk: str) -> bool:
        # cached results ("(cached)") didn't run anything, so they don't count
        return bool(_GO_PKG_TIMED.search("\n".join(logparse.clean(x) for x in chunk.splitlines())))

    def parse_results(self, output: str) -> dict[str, str]:
        return {f"{pkg}::{test}": outcome for pkg, test, outcome in _go_scan(output)}


RUNNERS = {r.name: r for r in (Pytest(), GoTest())}


def get(name: str):
    return RUNNERS.get(name or "pytest", RUNNERS["pytest"])


def parse_any(text: str) -> logparse.Failure:
    """The failures in a CI log, from whichever runner produced them."""
    for r in RUNNERS.values():
        f = r.parse_log(text)
        if f:
            f.runner = r.name
            return f
    return logparse.Failure()
