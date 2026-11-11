"""ci-red-to-green: a red GitHub Actions run becomes a fix PR you approve from chat,
offered only after Hermes's own pytest output proves the fix.

Hermes wiring only; the logic is in engine.py.
"""

from __future__ import annotations

import contextvars
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# The engine for this gateway process; the Desktop backend (dashboard/plugin_api.py) uses the same one.
_ENGINE = None

TOKEN_ENV = "CI_RED_TO_GREEN_GITHUB_TOKEN"
USAGE = (
    "/ci demo                       try it now: a real humanize bug, no token or GitHub needed\n"
    "/ci watch owner/repo [branch]  watch a branch (add more repos the same way)\n"
    "/ci unwatch owner/repo         stop watching one repo\n"
    "/ci                            show the fix proof and open the PR\n"
    "/ci status                     what I'm watching\n"
    "/ci discard                    drop the current incident\n"
    "/ci stop                       stop watching"
)


def _token():
    from agent.secret_scope import get_secret

    return get_secret(TOKEN_ENV)


def register(ctx):
    from plugins.plugin_storage import plugin_data_dir

    from .engine import Engine

    global _ENGINE
    def post(text: str) -> None:
        try:
            ctx.post_message(text)
        except Exception:  # no chat bound (started from Desktop): the pane still shows it
            logger.info("ci-red-to-green: no chat to post to")

    def changed(payload: dict) -> None:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event("ci-red-to-green", "state.changed", payload)

    engine = Engine(plugin_data_dir("ci-red-to-green"), token_fn=_token, post=post, on_change=changed)
    _ENGINE = engine

    def ci(raw_args: str, **_kw) -> str:
        args = (raw_args or "").split()
        sub = args[0].lower() if args else ""
        if sub == "demo":
            return engine.start_demo() + "\n\nReply fix to start."
        if sub == "watch":
            if len(args) < 2 or "/" not in args[1]:
                return "Usage: /ci watch owner/repo [branch]"
            branch = args[2] if len(args) > 2 else "main"
            try:
                msg = engine.start_watch(args[1], branch)
            except Exception as e:  # GitHubError, network
                return f"Couldn't start watching: {e}"
            # The poller thread inherits this chat's binding so it can post here later.
            bound = contextvars.copy_context()
            engine.start_thread(runner=lambda fn: bound.run(fn))
            return msg
        if sub != "stop" and engine.watch and not engine.watch.demo and not engine.thread_alive():
            # The watcher thread dies with the gateway; any /ci in this chat re-arms it here.
            bound = contextvars.copy_context()
            engine.start_thread(runner=lambda fn: bound.run(fn))
        if sub == "unwatch":
            if len(args) < 2:
                return "Usage: /ci unwatch owner/repo"
            return engine.unwatch(args[1])
        if sub == "discard":
            return engine.discard()
        if sub == "stop":
            engine.stop_thread()
            return "Stopped watching."
        if sub in ("status", "help") or engine.incident is None or engine.incident.status not in ("fixing", "ready"):
            return engine.status() + ("\n\n" + USAGE if sub == "help" or not engine.watch else "")
        ctx.post_message(engine.card_text())
        if not engine.proof()["accepted"]:
            return "No PR yet. Once the proof above is complete, send /ci again."
        pick = ctx.ask_choice("Open the PR? It has source files only and never touches the base branch.", [
            {"value": "open", "label": "Open PR"},
            {"value": "discard", "label": "Discard"},
        ])
        if pick == "open":
            try:
                return engine.open_pr()
            except Exception as e:
                return f"Couldn't open the PR: {e}"
        if pick == "discard":
            return engine.discard()
        return "No choice made. Send /ci again when you're ready."

    def on_pre_llm_call(user_message: str = "", **_kw):
        if engine.wants_fix(user_message):
            return {"context": engine.fix_context()}
        return None

    def on_pre_tool_call(tool_name: str = "", args: dict | None = None, **_kw):
        verdict = engine.needs_approval(tool_name, args or {})
        if verdict == "approve":
            cmd = (args or {}).get("command", "")
            return {"action": "approve", "message": f"ci-red-to-green: first command that runs this repo's code in the fix folder: {cmd[:300]}"}
        if verdict == "block":
            return {"action": "block", "message": "ci-red-to-green: no pushes from the fix worktree. The user opens the PR with /ci."}
        return None

    def on_post_tool_call(tool_name: str = "", args: dict | None = None, result: str = "", **_kw):
        try:
            engine.observe(tool_name, args or {}, result)
        except Exception:
            logger.exception("ci-red-to-green: could not record a test run")

    skill = Path(__file__).parent / "skills" / "fix-red-ci" / "SKILL.md"
    if skill.exists():
        ctx.register_skill("fix-red-ci", skill)
    ctx.register_automation_blueprint(
        "morning-ci",
        title="Morning CI check",
        description="Every weekday, check a repo's GitHub Actions on one branch and report anything red that has no fix PR yet.",
        category="work",
        schedule_template="{minute} {hour} * * 1-5",
        prompt_template=(
            "Using whatever GitHub access you have (the gh CLI or a GitHub tool), look at the GitHub Actions runs on {branch} of {repo} from the last 24 hours; if you have none, say so and stop. "
            "List each red run with its failing tests, and say whether an open pull request "
            "from a ci-fix/ branch already addresses it. If anything is red and unfixed, say so first "
            "and suggest sending /ci watch {repo} {branch} so ci-red-to-green can prove a fix."
        ),
        slots=[
            {"name": "repo", "type": "text", "label": "Which repo (owner/name)?", "default": "owner/repo"},
            {"name": "branch", "type": "text", "label": "Which branch?", "default": "main"},
            {"name": "time", "type": "time", "label": "What time?", "default": "08:30"},
        ],
        skills=["ci-red-to-green:fix-red-ci"],
        tags=["ci", "github", "daily"],
    )
    ctx.register_command("ci", ci, description="Turn red CI into a proven fix PR", args_hint="[demo|watch owner/repo [branch]|unwatch owner/repo|status|discard|stop]")
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
