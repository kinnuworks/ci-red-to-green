"""Backend routes for the Desktop pane: /api/plugins/ci-red-to-green/...

Runs inside the gateway process, next to the agent half, and uses the same engine
instance, so Telegram, the watcher and the pane always agree on the incident.
"""

from __future__ import annotations

import pathlib
import sys

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
_INIT = pathlib.Path(__file__).resolve().parents[1] / "__init__.py"


def _engine():
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            same = pathlib.Path(f).resolve() == _INIT
        except OSError:
            continue
        if same and getattr(mod, "_ENGINE", None) is not None:
            return mod._ENGINE
    raise HTTPException(status_code=503, detail="ci-red-to-green is not loaded in this gateway; enable the plugin and restart it")


@router.get("/state")
def state():
    return _engine().snapshot()


@router.post("/open-pr")
def open_pr():
    eng = _engine()
    try:
        return {"message": eng.open_pr(), **eng.snapshot()}
    except Exception as e:  # GitHub errors come back as text for the pane
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/discard")
def discard():
    eng = _engine()
    return {"message": eng.discard(), **eng.snapshot()}


@router.post("/demo")
def start_demo():
    eng = _engine()
    return {"message": eng.start_demo(), **eng.snapshot()}


class WatchBody(BaseModel):
    repo: str
    branch: str = "main"


@router.post("/watch")
def watch(body: WatchBody):
    eng = _engine()
    if body.repo.count("/") != 1:
        raise HTTPException(status_code=400, detail="Use owner/repo")
    try:
        msg = eng.start_watch(body.repo.strip(), (body.branch or "main").strip())
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    eng.start_thread()  # alerts show here; a /ci in a chat binds that chat for messages
    return {"message": msg, **eng.snapshot()}
