"""Read a failed GitHub Actions job log and find the pytest tests that failed.

Pure functions, no network. The watcher hands in the raw log text.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

# Actions prefixes every log line with an ISO timestamp: "2026-11-11T06:01:02.1234567Z ".
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ")
# Colour codes survive in some runners' logs.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# pytest's short summary: "FAILED tests/test_x.py::test_y - AssertionError: ..."
# A test id may carry parameters in brackets, and those can contain spaces: "test_x[1.0 MB]".
TEST_ID = r"[^\s\[]+?\.py(?:::[^\s\[]+(?:\[[^\]]*\])?)?"
_FAILED = re.compile(r"^(?:FAILED|ERROR) (?P<id>" + TEST_ID + r")(?: - (?P<why>.*))?$")
_SUMMARY = re.compile(r"=+ .*\b(\d+) failed\b.*=+")


@dataclass
class Failure:
    test_ids: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    runner: str = "pytest"

    @property
    def command(self) -> str:
        """The command that reproduces exactly these failures locally."""
        ids = " ".join(shlex.quote(t) for t in self.test_ids)
        return f"python -m pytest -rA {ids}".strip()

    def __bool__(self) -> bool:  # an empty Failure means "nothing we can reproduce"
        return bool(self.test_ids)


def clean(line: str) -> str:
    return _ANSI.sub("", _TS.sub("", line)).rstrip()


def parse_log(text: str) -> Failure:
    out = Failure()
    for raw in text.splitlines():
        line = clean(raw)
        m = _FAILED.match(line)
        if m:
            tid = m.group("id")
            if tid not in out.test_ids:
                out.test_ids.append(tid)
                out.reasons[tid] = (m.group("why") or "").strip()
            continue
        if _SUMMARY.match(line):
            out.summary = line.strip("= ").strip()
    return out
