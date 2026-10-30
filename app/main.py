"""
API entry point. OptiMind normally starts it through `run.py` (the installed
app's OptiMind.exe), which also starts the tray icon and the window, but you
can run just this with:

    uvicorn app.main:app --port 8765

to test the API/chat without the rest of the app (changes then need
OPTIMIND_OPEN_API=true: otherwise only OptiMind's own window may make
them, see app/service/access.py).
"""
from __future__ import annotations

import asyncio
import logging
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException

from app import __version__, i18n
from app.agent import focus, games
from app.agent.optimizer import restore_all
from app.api import app_routes, drivers_routes, games_routes
from app.api.routes import router
from app.config import FROZEN
from app.service import access
from app.service.orchestrator import run_forever

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

_STATIC_DIR = Path(__file__).parent / "static"
_LOCAL_HOSTS = {"127.0.0.1", "localhost"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(run_forever())
    try:
        yield
    finally:
        task.cancel()
        focus.stop()  # first, so no switch boosts an app after everything is put back
        games.stop()  # the game session so far is saved, PresentMon stopped
        restore_all()  # apps the agent was holding back get their own settings back


class LocalizedResponse(JSONResponse):
    """The API's answers in OptiMind's language: each text the agent made (app/i18n) is translated
    as it's sent, while the agent itself, its log and the AI keep working in English."""

    def render(self, content) -> bytes:
        return super().render(i18n.localize(content))


app = FastAPI(
    title="OptiMind AI",
    description="Local CPU/RAM optimization agent for Windows 11, with AI supervising the policies.",
    version=__version__,
    lifespan=lifespan,
    default_response_class=LocalizedResponse,
    # The interactive API documentation is for developers: from source only, not in the installed app
    docs_url=None if FROZEN else "/docs",
    redoc_url=None if FROZEN else "/redoc",
    openapi_url=None if FROZEN else "/openapi.json",
)


@app.exception_handler(HTTPException)
async def translated_error(request: Request, exc: HTTPException) -> Response:
    """An error the page shows (a toast, a dialog) in OptiMind's language too."""
    exc.detail = i18n.localize(exc.detail)
    return await http_exception_handler(request, exc)


@app.middleware("http")
async def only_from_this_pc(request: Request, call_next):
    """The API changes things on this PC (with administrator rights, when
    OptiMind has them), so a website open in your browser must not be able
    to use it: its requests carry another site's Origin (a page posting to
    127.0.0.1) or Host (a "DNS rebinding" trick) and are refused. Anything
    that changes something also needs the secret only OptiMind's own window
    has (app/service/access.py): other programs and other Windows accounts
    on this PC can read, but not change."""
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if host.split(":")[0] not in _LOCAL_HOSTS or (origin and origin != f"http://{host}"):
        return LocalizedResponse({"detail": i18n.tx("Refused: only OptiMind's own pages on this PC may use this API.")},
                                 status_code=403)
    if not access.allowed(request.method, request.url.path, request.headers.get(access.HEADER)):
        return LocalizedResponse({"detail": i18n.tx("Refused: only OptiMind's own window may change things.")}, status_code=403)
    return await call_next(request)


app.include_router(router, prefix="/api")
app.include_router(app_routes.router, prefix="/api")
app.include_router(games_routes.router, prefix="/api")
app.include_router(drivers_routes.router, prefix="/api")


def _page() -> bytes:
    """The dashboard page. The installed app keeps it inside OptiMind.exe, minified and
    compressed (packaging/protect.py); from source it is app/static/dashboard.html."""
    try:
        import optimind_page  # only in the installed app
    except ImportError:
        return (_STATIC_DIR / "dashboard.html").read_bytes()
    return zlib.decompress(optimind_page.PAGE)


@app.get("/", include_in_schema=False)
def dashboard() -> Response:
    """The dashboard: one self-contained page that works offline (OptiMind's window shows it), in
    OptiMind's language (app/i18n)."""
    return Response(i18n.inject(_page()), media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(_STATIC_DIR / "optimind.ico")
