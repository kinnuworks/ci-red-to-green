# How do you trust an agent's CI fix?

An agent that says "fixed it, tests pass" is asking you to take its word. Models write that sentence even when the tests never ran, when they ran the wrong tests, or when the fix was to edit the test. So ci-red-to-green doesn't take the agent's word for anything. It reads the test runner's own output from Hermes's terminal tool and decides by itself whether a pull request is earned.

## Three points, not one

A fix PR is offered only when the record shows this for every failing test:

1. **Green before.** With only the source files changed since the last green run put back as they were, the test passes. That shows the regression came in with those commits, not from the environment or a flaky test.
2. **Red now.** On the red commit, before any source changes, the test fails, twice.
3. **Green with the fix.** After the change, the test passes three runs in a row, and a full-suite run shows no test that passed before now fails.

Here is the table from a real run, [PR #4](https://github.com/kinnuworks/datekit-demo/pull/4). A commit changed `calendar.monthrange(year, month)[1]` to `[0]`, the weekday of the first day instead of the number of days, and five tests went red:

| test | at the last green commit (cf44f73) | before the fix | after the fix |
|---|---|---|---|
| `test_days_in_month_leap` | PASSED | FAILED 2/2 runs | PASSED 3/3 runs in a row |
| `test_add_months_simple` | PASSED | FAILED 2/2 runs | PASSED 3/3 runs in a row |
| `test_add_months_clamps_to_month_end` | PASSED | FAILED 2/2 runs | PASSED 3/3 runs in a row |
| `test_add_months_across_year` | PASSED | FAILED 2/2 runs | PASSED 3/3 runs in a row |
| `test_add_months_negative` | PASSED | FAILED 2/2 runs | PASSED 3/3 runs in a row |

From the red run to GitHub saying green on the fix took about five minutes, including a network blip while cloning.

## What the plugin checks, and what it refuses

- **Only real runs count.** The terminal call has to be the test command itself: `cd`, `source` or variables may come before it, nothing after it, no pipes or redirection. Only the lines between the runner's own session header and summary are read, so a model that echoes `PASSED` gets nothing.
- **It knows what state each run was in.** For every run, the plugin hashes the fix folder against the red commit and the last green commit. A run made with the source restored to green counts as "green before". A run made while any test file or test config is edited never counts, even if the edit is undone later.
- **One pass before the fix means flaky.** If a failing test passes even once before any change, the card says flaky and no PR is offered.
- **The PR is built by code, not the model.** The plugin makes the commit from changed source files only. Tests, `testdata`, `conftest.py`, pytest/tox/setup config, `.github` and anything your `.gitignore` files match never go in, so GitHub runs your original tests against the fix.
- **GitHub has the last word.** After Open PR, the plugin waits for every workflow on the PR. "Done" means GitHub says green, and nothing is ever merged or pushed to your base branch.
- **You approve the first command that runs your code.** The plugin escalates it to Hermes's approval prompt, with the command shown, whether it's an install or the first test run.

## Why this has to be a plugin

A prompt or a skill can ask the model to do all of the above. It can't make the model do it, and it can't check. The plugin sits in Hermes's hooks: it sees each terminal command before it runs (to ask for approval or block a push), sees each result after (to record it), and owns the PR button. The watcher that notices red runs uses plain GitHub API calls and no model calls.

## Try it

```bash
hermes plugins install ci-red-to-green --enable
```

Then send `/ci demo` in any chat with Hermes. It replays a real bug from humanize (its PR #329, `naturalsize(999999)` giving `1000.0 kB`) on a bundled copy with humanize's own tests, with no token and no network.
