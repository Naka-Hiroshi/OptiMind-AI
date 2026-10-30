"""The Games page: game sessions (frame rates, what held each game back, advice, your
notes), the installed games and which drive they're on, "Measure now", and what to
close before you play."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app import i18n
from app.agent import games
from app.agent.logger import log_action
from app.api import LocalizedRoute
from app.config import IS_WINDOWS
from app.models import GameNoteRequest
from app.service.orchestrator import _engine
from app.system import gpu, gpu_optimization

router = APIRouter(route_class=LocalizedRoute)


@router.get("/games")
def get_games(sessions: int = 30) -> dict:
    """The game being played (live), the last sessions, and the game library."""
    return {"status": games.status(), "sessions": games.sessions(max(1, min(sessions, 200))),
            "library": games.library()}


@router.get("/games/before-play")
def get_before_play() -> dict:
    """Memory now, the apps holding the most memory of their own, and recorders that recorded your last games."""
    return games.before_play()


@router.post("/games/note")
def set_game_note(req: GameNoteRequest) -> dict:
    """Your note on a game session (what you changed for it); an empty note removes it."""
    try:
        return {"session": games.set_note(req.started, req.exe, req.note)}
    except LookupError as e:
        raise HTTPException(status_code=404, detail=i18n.message(e))


@router.post("/games/measure")
def measure_game() -> dict:
    """Measures the running game's frame rate for 60 seconds (PresentMon)."""
    try:
        return games.measure_now()
    except LookupError as e:
        raise HTTPException(status_code=409, detail=i18n.message(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=i18n.message(e))


@router.get("/games/optimization")
def get_game_optimization() -> dict:
    """Game-GPU optimization status: whether it's enabled, what's been applied
    (shader cache, Game Mode), and the GPU's current reading."""
    return {
        "enabled": _engine._game_opt.is_enabled(),
        "applied": _engine._game_opt.status()["applied"],
        "shader_cache": gpu_optimization.shader_cache_enabled(),
        "game_mode": gpu_optimization.game_mode_enabled(),
        "hags": gpu_optimization.hags_available(),
        "gpu": gpu.primary() if IS_WINDOWS else None,
    }


@router.post("/games/optimization/enable")
def enable_game_optimization() -> dict:
    """Turn on automatic game-GPU optimization (shader cache, Game Mode when a
    game is in focus with high GPU usage)."""
    _engine._game_opt.enable()
    return {"enabled": True}


@router.post("/games/optimization/disable")
def disable_game_optimization() -> dict:
    """Turn off automatic game-GPU optimization and put back any changes it made."""
    _engine._game_opt.disable()
    for entry in _engine._game_opt.restore_all():
        log_action(entry)
    return {"enabled": False}
