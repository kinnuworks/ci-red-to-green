"""End to end through Hermes's real plugin manager: discover the plugin from a fresh
HERMES_HOME, run `/ci demo`, then drive the real hooks the way the agent loop does
(pre_llm_call on "fix", pre_tool_call on the first test run, post_tool_call with the
terminal tool's real result JSON) and open the demo PR.

Needs a Hermes checkout: set HERMES_AGENT_DIR, plus HERMES_PYTHON and HERMES_SITE when the
current interpreter can't import Hermes's dependencies.
Skipped otherwise, so the plain unit-test CI stays dependency-free.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

AGENT = os.environ.get("HERMES_AGENT_DIR")
PLUGIN = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(not AGENT, reason="set HERMES_AGENT_DIR to run against a Hermes checkout")

SCRIPT = r'''
import json, os, re, subprocess, sys
from hermes_cli.plugins import discover_plugins, get_plugin_command_handler, invoke_hook, get_pre_tool_call_directive

discover_plugins()
from hermes_cli.plugins import get_plugin_manager
_pm = get_plugin_manager()
ci = get_plugin_command_handler("ci")
out = {"skills": sorted(str(k) for k in _pm._plugin_skills), "blueprints": sorted(str(k) for k in _pm._automation_blueprints)}
out["demo"] = ci("demo")
ctx = [r for r in invoke_hook("pre_llm_call", user_message="fix", session_id="s", conversation_history=[]) if r]
out["context"] = ctx[0]["context"] if ctx else ""
wt = re.search(r"in (\S+/worktrees/\d+)", out["context"]).group(1)
cmd = re.search(r"twice, as two separate terminal calls in \S+: (.+)\n", out["context"]).group(1)
full = f"cd {wt} && {cmd}"
out["clone_directive"] = get_pre_tool_call_directive("terminal", {"command": f"test -d {wt} && echo exists"})[0]
out["first_run_directive"] = get_pre_tool_call_directive("terminal", {"command": full})[0]
out["push_directive"] = get_pre_tool_call_directive("terminal", {"command": f"git -C {wt} push origin HEAD"})[0]

def run_tests():
    p = subprocess.run(full.replace("python -m pytest", os.environ["PYTEST_PYTHON"] + " -m pytest", 1),
                       shell=True, capture_output=True, text=True)
    invoke_hook("post_tool_call", tool_name="terminal", args={"command": full},
                result=json.dumps({"output": p.stdout + p.stderr, "exit_code": p.returncode, "error": None}),
                session_id="s", tool_call_id="t")
    return p.returncode

out["before"] = [run_tests(), run_tests()]
src = open(f"{wt}/humanize_mini/filesize.py").read()
src = src.replace('    space = "" if gnu else " "\n',
                  '    if exp < len(suffix) and abs(float(format % (abs_bytes / (base**exp)))) >= base:\n'
                  '        exp += 1\n    space = "" if gnu else " "\n')
open(f"{wt}/humanize_mini/filesize.py", "w").write(src)
out["after"] = [run_tests() for _ in range(3)]
out["card"] = ci("status")
from importlib import import_module
eng = [m for m in sys.modules.values() if getattr(m, "_ENGINE", None) is not None and getattr(m, "__file__", "").endswith("ci-red-to-green/__init__.py")][0]._ENGINE
out["status"] = eng.incident.status
out["open"] = eng.open_pr()
out["patch"] = open(eng.incident.pr_url).read()
print("RESULT " + json.dumps(out))
'''


def test_demo_through_the_real_plugin_manager(tmp_path):
    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN, home / "plugins" / "ci-red-to-green",
                    ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "docs", ".pytest_cache", "_hermes"))
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - ci-red-to-green\n")
    # HERMES_SITE: the site-packages Hermes runs with (`hermes --run-module site` prints it)
    paths = [AGENT] + ([os.environ["HERMES_SITE"]] if os.environ.get("HERMES_SITE") else [])
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONPATH=os.pathsep.join(paths))
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"  # use this runtime as is; no per-data-root relaunch
    env["PYTEST_PYTHON"] = sys.executable  # runs the demo's tests; the hooks see the agent's command
    py = os.environ.get("HERMES_PYTHON", sys.executable)
    p = subprocess.run([py, "-c", SCRIPT], cwd=AGENT, env=env, capture_output=True, text=True, timeout=300)
    line = next((ln[ln.index("RESULT "):] for ln in (p.stdout + "\n" + p.stderr).splitlines() if "RESULT {" in ln), None)
    assert line, py + "\n" + p.stdout[-2000:] + p.stderr[:2500]
    out = json.loads(line[len("RESULT "):])
    assert "ci-red-to-green:fix-red-ci" in out["skills"]
    assert "ci-red-to-green:morning-ci" in out["blueprints"]
    assert "PR #329" in out["demo"]
    assert "Reproduce BEFORE changing anything" in out["context"]
    assert out["clone_directive"] is None
    assert out["first_run_directive"] == "approve"
    assert out["push_directive"] == "block"
    assert out["before"] == [1, 1] and out["after"] == [0, 0, 0]
    assert out["status"] == "ready"
    assert "patch" in out["open"]
    assert "+++ b/humanize_mini/filesize.py" in out["patch"] and "tests/" not in out["patch"]
