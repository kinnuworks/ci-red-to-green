# Eval: six real humanize regressions

To see ci-red-to-green work on a real project instead of a toy repo, I replayed six real bugs from [humanize](https://github.com/python-humanize/humanize) on a fork, [kinnuworks/humanize](https://github.com/kinnuworks/humanize), one after another. Each bug is one that humanize itself fixed in a past pull request. Its test suite has 878 tests.

## How each replay ran

1. Push a commit to the fork's `main` that reverts only the source half of the original fix. The test that came with the fix stays, so humanize's own CI goes red. The commit message says it's a replay.
2. ci-red-to-green, watching the fork from Telegram, posts the alert: the failing tests, the assertion and the commit since the last green run.
3. Reply `fix`. Hermes clones the red commit, installs humanize the way its CI does, and works under the plugin's gate. The first command that runs the repo's code goes to Hermes's approval prompt.
4. When the proof is complete, `/ci` → Open PR. The plugin builds the commit from source files only and follows the PR's CI.
5. When GitHub says green, fast-forward the fix into `main` and start the next replay.

## Results

| # | Real humanize bug | Failing tests | Proof on the card | PR | Files in the PR | GitHub CI on the PR | Red CI to green PR |
|---|---|---|---|---|---|---|---|
| 1 | [#329](https://github.com/python-humanize/humanize/pull/329) `naturalsize` rounding rollover | 6 | PASSED at last green · FAILED 2/2 · PASSED 3/3 · suite 878 passed | [PR #1](https://github.com/kinnuworks/humanize/pull/1) | `src/humanize/filesize.py` | green | 30 min* |
| 2 | [#346](https://github.com/python-humanize/humanize/pull/346) `intword` rounding carry | 1 | same | [PR #2](https://github.com/kinnuworks/humanize/pull/2) | `src/humanize/number.py` | green | 13 min* |
| 3 | [#359](https://github.com/python-humanize/humanize/pull/359) `metric` extra digit on carry | 5 | same | [PR #3](https://github.com/kinnuworks/humanize/pull/3) | `src/humanize/number.py` | green | 7 min |
| 4 | [#354](https://github.com/python-humanize/humanize/pull/354) `fractional` rounds to a whole | 5 | same | [PR #4](https://github.com/kinnuworks/humanize/pull/4) | `src/humanize/number.py` | green | 6 min |
| 5 | [#295](https://github.com/python-humanize/humanize/pull/295) `naturaldelta` truncates years | 2 | same | [PR #5](https://github.com/kinnuworks/humanize/pull/5) | `src/humanize/time.py` | green | 6 min |
| 6 | [#392](https://github.com/python-humanize/humanize/pull/392) `intcomma` overflow on big ints | 2 | same | [PR #6](https://github.com/kinnuworks/humanize/pull/6) | `src/humanize/number.py` | green | 4.5 min |

6 of 6 replays ended in a proven PR that changed one source file and went green on humanize's own CI. None touched a test.

"Red CI to green PR" runs from the end of the red CI run to the end of the PR's CI run, and includes the human steps (reply `fix`, approve, `/ci`, Open PR).

\* Runs 1 and 2 were slower for reasons on our side, both described under "What the eval caught".

## Honest notes

- **These are replays, not live bugs.** The fork still has humanize's history, so the agent could read the original fix. Four of the six fixes match humanize's own fix; for #295 and #392 the agent wrote a different fix, and both passed humanize's CI. The point of the eval is the plugin's gate, not the model's ingenuity.
- **CI changes on the fork.** Two commits changed only the fork's workflow, not source or tests. The jobs run on Ubuntu only (9 Python versions instead of 27 jobs), so a replay doesn't wait in the macOS and Windows queue. The translations are compiled with `polib` instead of `gettext`, because GitHub's runners couldn't reach the Ubuntu package mirrors that day. Unrelated workflows (benchmarks, docs, release and label bots, lint) are disabled on the fork.
- **Approvals.** I approved each install and first test run in Telegram myself, as the person running the eval.

## What the eval caught (and the fixes)

- **Nested brackets in test ids.** humanize names parametrized tests like `test_metric[[9999]-10.0 k]`. The log parser stopped at the first `]` and reported "no failing test" for replay 3. Fixed in `8cd41de`, with humanize's real log lines as a test; replay 3 then ran normally.
- **Approval after a timeout.** In replay 1 the first approval prompt timed out, and a change from earlier that day kept the plugin from ever marking the approval as used, so the next command asked again. Fixed in `3e3a99b`: the approval counts as used when it's asked, and a `BLOCKED:` result from Hermes (denied or timed out) resets it.
- **A skipped full suite.** In replay 2 the agent didn't run the full suite before and after. The gate didn't say "ready" until it did; one message asking for the two runs was enough. Working as designed, but it cost time.
