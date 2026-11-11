"""ci-red-to-green: a red GitHub Actions run becomes a fix PR you approve from chat,
offered only after Hermes's own pytest output proves the fix.

Hermes wiring only; the logic is in engine.py.
"""

from __future__ import annotations

import contextvars
import logging

logger = logging.getLogger(__name__)

TOKEN_ENV = "CI_RED_TO_GREEN_GITHUB_TOKEN"
USAGE = (
    "/ci watch owner/repo [branch]  start watching a branch\n"
    "/ci                            show the fix proof and open the PR\n"
    "/ci status                     what I'm watching\n"
    "/ci stop                       stop watching"
)


def _token():
    from agent.secret_scope import get_secret

    return get_secret(TOKEN_ENV)


def register(ctx):
    from plugins.plugin_storage import plugin_data_dir

    from .engine import Engine

    engine = Engine(plugin_data_dir("ci-red-to-green"), token_fn=_token, post=lambda text: ctx.post_message(text))

    def ci(raw_args: str, **_kw) -> str:
        args = (raw_args or "").split()
        sub = args[0].lower() if args else ""
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
        if sub == "stop":
            engine.stop_thread()
            return "Stopped watching."
        if sub in ("status", "help") or engine.incident is None or engine.incident.status not in ("fixing", "ready"):
            return engine.status() + ("\n\n" + USAGE if sub == "help" or not engine.watch else "")
        ctx.post_message(engine.card_text())
        if not engine.proof()["accepted"]:
            return "No PR yet. Once the proof above is complete, send /ci again."
        pick = ctx.ask_choice("Open the PR?", [
            {"value": "open", "label": "Open PR", "description": "source files only; never pushes to the base branch"},
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
            return {"action": "approve", "message": "ci-red-to-green: first test run in the fix worktree"}
        if verdict == "block":
            return {"action": "block", "message": "ci-red-to-green: no pushes from the fix worktree. The user opens the PR with /ci."}
        return None

    def on_post_tool_call(tool_name: str = "", args: dict | None = None, result: str = "", **_kw):
        try:
            engine.observe(tool_name, args or {}, result)
        except Exception:
            logger.exception("ci-red-to-green: could not record a test run")

    ctx.register_command("ci", ci, description="Turn red CI into a proven fix PR", args_hint="[watch owner/repo [branch]|status|stop]")
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
