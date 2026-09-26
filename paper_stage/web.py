"""Aplicación web de Teatrito de Papel (Starlette + uvicorn, sin paso de compilación)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
from dataclasses import fields
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import core, library
from .jobs import TERMINAL, JobManager

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _request_from(payload: dict) -> core.VideoRequest:
    allowed = {f.name for f in fields(core.VideoRequest)} - {"slug", "extra"}
    data = {k: v for k, v in payload.items() if k in allowed and v not in (None, "")}
    if "max_turns" in data:
        data["max_turns"] = int(data["max_turns"])
    if "max_budget_usd" in data:
        data["max_budget_usd"] = float(data["max_budget_usd"])
    if not data.get("tema"):
        raise ValueError("Escribe un tema.")
    return core.VideoRequest(**data)


async def _json(request: Request) -> dict:
    try:
        payload = await request.json()
    except ValueError:
        raise ValueError("El cuerpo de la petición no es JSON válido.")
    if not isinstance(payload, dict):
        raise ValueError("Se esperaba un objeto JSON.")
    return payload


class TokenAuth(BaseHTTPMiddleware):
    """Si PAPER_STAGE_TOKEN está definido, exige ese token (cookie, cabecera o ?token=)."""

    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        given = (
            request.query_params.get("token")
            or request.cookies.get("ps_token")
            or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        )
        if not given or not hmac.compare_digest(given, self.token):
            return Response("Falta el token de acceso (?token=…).", status_code=401)
        if request.query_params.get("token"):
            response = RedirectResponse(request.url.remove_query_params("token"))
            response.set_cookie("ps_token", self.token, httponly=True, samesite="strict")
            return response
        return await call_next(request)


def create_app(manager: JobManager | None = None, token: str | None = None) -> Starlette:
    if manager is None:
        runner = None
        if os.environ.get("PAPER_STAGE_DEMO") == "1":
            from .demo import run_demo
            runner = run_demo
        manager = JobManager(
            core.DATA_DIR,
            runner=runner,
            concurrency=int(os.environ.get("PAPER_STAGE_CONCURRENCY", "1")),
        )
    demo = manager.runner is not core.run_agent

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await manager.start()
        yield
        await manager.stop()

    # ------------------------------------------------------------------ configuración

    async def config(request: Request):
        return JSONResponse({
            "models": core.MODELS, "default_model": core.DEFAULT_MODEL,
            "efforts": core.EFFORTS, "languages": core.LANGUAGES, "formats": core.FORMATS,
            "demo": demo, "concurrency": manager.concurrency,
        })

    async def health(request: Request):
        return JSONResponse(await asyncio.to_thread(core.check_env))

    async def prompt_preview(request: Request):
        try:
            req = _request_from(await _json(request))
            req.validate()
            return JSONResponse({"slug": req.slug, "prompt": core.render_prompt(req.variables()),
                                 "kickoff": core.kickoff_prompt(req)})
        except (ValueError, TypeError) as exc:
            return _error(str(exc))

    # ------------------------------------------------------------------ trabajos

    async def list_jobs(request: Request):
        return JSONResponse(await asyncio.to_thread(manager.list))

    async def create_job(request: Request):
        try:
            job = manager.create(_request_from(await _json(request)))
        except (ValueError, TypeError) as exc:
            return _error(str(exc))
        return JSONResponse(job, status_code=201)

    async def get_job(request: Request):
        job = manager.get(request.path_params["job_id"])
        return JSONResponse(job) if job else _error("Trabajo no encontrado.", 404)

    def _job_action(action):
        async def endpoint(request: Request):
            try:
                result = action(request.path_params["job_id"])
            except KeyError:
                return _error("Trabajo no encontrado.", 404)
            except ValueError as exc:
                return _error(str(exc), 409)
            return JSONResponse(result if result is not None else {"ok": True})
        return endpoint

    async def job_events(request: Request):
        job_id = request.path_params["job_id"]
        if not manager.store.get(job_id):
            return _error("Trabajo no encontrado.", 404)

        def fmt(event: dict) -> str:
            return f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

        async def stream():
            queue = manager.subscribe(job_id)
            try:
                last = -1
                for event in manager.events(job_id):
                    last = event["seq"]
                    yield fmt(event)
                job = manager.store.get(job_id)
                if job and job["status"] in TERMINAL:
                    yield "event: end\ndata: {}\n\n"
                    return
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if event["seq"] <= last:
                        continue
                    last = event["seq"]
                    yield fmt(event)
                    if event["kind"] == "status" and event["status"] in TERMINAL:
                        yield "event: end\ndata: {}\n\n"
                        return
            finally:
                manager.unsubscribe(job_id, queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ biblioteca

    async def list_videos(request: Request):
        return JSONResponse(await asyncio.to_thread(library.list_videos))

    async def get_video(request: Request):
        slug = request.path_params["slug"]
        try:
            data = await asyncio.to_thread(library.detail, slug)
        except ValueError:
            return _error("Video no válido.", 400)
        if not data:
            return _error("Video no encontrado.", 404)
        data["jobs"] = [j for j in manager.store.list() if j["slug"] == slug]
        return JSONResponse(data)

    async def delete_video(request: Request):
        slug = request.path_params["slug"]
        if slug in manager.active_slugs():
            return _error("Hay un trabajo en curso para este video.", 409)
        try:
            deleted = await asyncio.to_thread(library.delete, slug)
        except ValueError:
            return _error("Video no válido.", 400)
        return JSONResponse({"ok": True}) if deleted else _error("Video no encontrado.", 404)

    async def poster(request: Request):
        try:
            out_dir = library.slug_dir(request.path_params["slug"])
        except ValueError:
            return _error("Video no válido.", 400)
        image = await asyncio.to_thread(library.poster, out_dir)
        return FileResponse(image) if image else _error("Sin portada.", 404)

    async def output_file(request: Request):
        try:
            out_dir = library.slug_dir(request.path_params["slug"]).resolve()
        except ValueError:
            return _error("Video no válido.", 400)
        path = (out_dir / request.path_params["path"]).resolve()
        if out_dir not in path.parents or not path.is_file() or path.suffix not in library.PUBLIC_SUFFIXES:
            return _error("Archivo no encontrado.", 404)
        return FileResponse(path)

    async def index(request: Request):
        return FileResponse(STATIC_DIR / "index.html")

    routes = [
        Route("/", index),
        Route("/api/config", config),
        Route("/api/health", health),
        Route("/api/prompt-preview", prompt_preview, methods=["POST"]),
        Route("/api/jobs", list_jobs),
        Route("/api/jobs", create_job, methods=["POST"]),
        Route("/api/jobs/{job_id}", get_job),
        Route("/api/jobs/{job_id}", _job_action(manager.delete), methods=["DELETE"]),
        Route("/api/jobs/{job_id}/cancel", _job_action(manager.cancel), methods=["POST"]),
        Route("/api/jobs/{job_id}/resume", _job_action(manager.resume), methods=["POST"]),
        Route("/api/jobs/{job_id}/events", job_events),
        Route("/api/videos", list_videos),
        Route("/api/videos/{slug}", get_video),
        Route("/api/videos/{slug}", delete_video, methods=["DELETE"]),
        Route("/poster/{slug}.jpg", poster),
        Route("/files/{slug}/{path:path}", output_file),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
        Mount("/fonts", StaticFiles(directory=core.FONTS_DIR, check_dir=False), name="fonts"),
    ]
    token = token if token is not None else os.environ.get("PAPER_STAGE_TOKEN")
    middleware = [Middleware(TokenAuth, token=token)] if token else []
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.manager = manager
    return app
