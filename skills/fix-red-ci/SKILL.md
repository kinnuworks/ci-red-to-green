---
name: fix-red-ci
description: Fix a failing pytest test so ci-red-to-green can prove it and offer a PR. Load when the user replies "fix" to a red CI alert, or asks why the proof card isn't ready.
---

# Fix red CI with proof

ci-red-to-green records pytest's own output from your terminal calls and offers a pull request
only when the record shows all of this:

| Check | What counts |
|---|---|
| Failed before | each failing test FAILED in 2 runs before any source file changed |
| Not flaky | the test never PASSED before the change (one pass marks it flaky and blocks the PR) |
| Fixed | each test PASSED in 3 runs in a row after the change |
| Nothing else broke | a full-suite run before and after; no test that passed before fails after |
| Tests untouched | tests, conftest.py, pytest/tox/setup config and .github are never put in the PR |

## How to work

1. Work only in the fix folder the alert names. Clone it there if it's missing; never in the user's checkout.
2. Install the project the way its CI does. The first command that runs the repo's code asks the user for approval.
3. Reproduce first: run the exact pytest command from the alert twice, each as its own terminal call.
4. Run the full suite once (`python -m pytest -rA`) before changing anything.
5. If the alert lists commits since the last green run, read their diff first. The cause is usually there.
6. Fix the source. Never edit tests to make them pass; a fix that needs a test change is refused.
7. Run the failing tests three times, then the full suite once.
8. Don't commit or push (pushes from the fix folder are blocked). Tell the user the cause in one line and to send `/ci` (or use the CI pane in Desktop) to see the proof and open the PR.

Always pass `-rA`, and run each pytest command as its own terminal call: no loops, no pipes such as `| tail`.
Only real pytest runs count; text that merely looks like pytest output is ignored.

## When the card says "Not ready"

- `0/2 failing runs before`: you changed source before reproducing. Undo the change (`git stash`), reproduce twice, then reapply it (`git stash pop`).
- `flaky`: the test passed before any change. Tell the user it is flaky, not broken. Don't open a PR.
- `new failures in the full suite`: your fix broke another test. Fix that too, or rethink the change.
- `test or CI files changed`: revert those edits. Only source files ship.
