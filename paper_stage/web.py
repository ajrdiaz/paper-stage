"""Aplicación web de Teatrito de Papel (Starlette + uvicorn, sin paso de compilación)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
from dataclasses import fields
from pathlib import Path
from typing import Callable

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import core, library
from .jobs import TERMINAL, JobManager
from .publishing import TEST_SLUG, PublishError, PublishRequest, Publisher, default_text, text_units

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _request_from(payload: dict) -> core.VideoRequest:
    # El personaje llega como personaje_id y lo copia el gestor (prepare), nunca el cliente.
    allowed = {f.name for f in fields(core.VideoRequest)} - {"slug", "extra", "personaje", "referencias"}
    data = {k: v for k, v in payload.items() if k in allowed and v not in (None, "")}
    if "max_turns" in data:
        data["max_turns"] = int(data["max_turns"])
    if "max_budget_usd" in data:
        data["max_budget_usd"] = float(data["max_budget_usd"])
    if not data.get("tema"):
        raise ValueError("Escribe un tema.")
    return core.VideoRequest(**data)


def _character_from(payload: dict) -> core.Character:
    allowed = {f.name for f in fields(core.Character)} - {"id"}
    try:
        return core.Character(**{k: v for k, v in payload.items() if k in allowed})
    except TypeError:
        raise ValueError("Faltan datos del personaje: nombre, nicho, apariencia, personalidad, "
                         "escenario y paleta.")


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


def create_app(manager: JobManager | None = None, token: str | None = None,
               suggester: Callable | None = None, character_suggester: Callable | None = None,
               publisher: Publisher | None = None) -> Starlette:
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
    if suggester is None:
        if demo:
            from .demo import suggest_demo as suggester
        else:
            suggester = core.suggest_topics
    if publisher is None:
        parts = {}
        if demo:
            from .demo import demo_publisher_parts
            parts = demo_publisher_parts(manager.data_dir)
        publisher = Publisher(manager.store.db, **parts)
    if character_suggester is None:
        if demo:
            from .demo import suggest_characters_demo as character_suggester
        else:
            character_suggester = core.suggest_characters

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await manager.start()
        yield
        await publisher.stop()
        await manager.stop()

    # ------------------------------------------------------------------ configuración

    async def config(request: Request):
        return JSONResponse({
            "models": core.MODELS, "default_model": core.DEFAULT_MODEL,
            "efforts": core.EFFORTS, "languages": core.LANGUAGES, "formats": core.FORMATS,
            "demo": demo, "concurrency": manager.concurrency, "default_cta": core.DEFAULT_CTA,
            "voices": core.VOICES, "default_character": core.LIA.id,
        })

    async def health(request: Request):
        return JSONResponse(await asyncio.to_thread(core.check_env))

    async def prompt_preview(request: Request):
        try:
            payload = await _json(request)
            req = manager.prepare(_request_from(payload), payload.get("personaje_id"))
            return JSONResponse({"slug": req.slug, "prompt": core.render_prompt(req.variables()),
                                 "kickoff": core.kickoff_prompt(req)})
        except (ValueError, TypeError) as exc:
            return _error(str(exc))

    async def suggest(request: Request):
        """3 temas nuevos del nicho del personaje: ni ya producidos o en cola por él, ni entre
        los que ya se mostraron."""
        payload = await _json(request)
        character = manager.characters.get(payload.get("personaje_id") or core.LIA.id)
        if not character:
            return _error("Personaje no encontrado.", 404)
        idioma = payload.get("idioma") or character.idioma
        edad = str(payload.get("edad") or character.edad)
        if idioma not in core.LANGUAGES or not core.AGE_RE.match(edad):
            return _error("Idioma o edad no válidos.")
        pista = " ".join(str(payload.get("pista") or "").split())[:120]
        shown = [" ".join(str(t).split()) for t in (payload.get("evitar") or [])][:60]
        used = await asyncio.to_thread(manager.used_topics, idioma, character.id)
        try:
            ideas = await suggester(character, idioma, edad, pista, used + shown, 6)
        except Exception as exc:  # noqa: BLE001 — credenciales, red o respuesta rara del modelo
            return _error(f"No se pudieron sugerir temas: {exc}", 502)
        taken = {core.topic_key(t, idioma) for t in used + shown}
        picked = []
        for idea in ideas:
            key = core.topic_key(idea["tema"], idioma)
            if key in taken or not 2 <= len(idea["tema"]) <= 200:
                continue
            if await asyncio.to_thread(manager.is_used, idea["tema"], idioma, character.id):
                continue
            taken.add(key)
            picked.append(idea)
            if len(picked) == 3:
                break
        if not picked:
            return _error("No salieron temas nuevos. Prueba otra vez o cambia la pista.", 502)
        return JSONResponse({"temas": picked})

    # ------------------------------------------------------------------ personajes

    def _character_json(character: core.Character, videos: list[dict]) -> dict:
        mine = [v for v in videos if v["personaje"] == character.id]
        done = [v for v in mine if v["has_video"]]
        return {**character.to_dict(), "hashtag": character.hashtag, "videos": len(mine),
                "ultimo_video": done[0]["slug"] if done else None}

    async def list_characters(request: Request):
        videos = await asyncio.to_thread(library.list_videos)
        return JSONResponse([_character_json(c, videos) for c in manager.characters.list()])

    async def get_character(request: Request):
        character = manager.characters.get(request.path_params["character_id"])
        if not character:
            return _error("Personaje no encontrado.", 404)
        return JSONResponse(_character_json(character, await asyncio.to_thread(library.list_videos)))

    async def create_character(request: Request):
        try:
            character = manager.characters.create(_character_from(await _json(request)))
        except ValueError as exc:
            return _error(str(exc))
        return JSONResponse(_character_json(character, []), status_code=201)

    async def update_character(request: Request):
        try:
            character = manager.characters.update(request.path_params["character_id"],
                                                  _character_from(await _json(request)))
        except KeyError:
            return _error("Personaje no encontrado.", 404)
        except ValueError as exc:
            return _error(str(exc))
        return JSONResponse(_character_json(character, await asyncio.to_thread(library.list_videos)))

    async def delete_character(request: Request):
        character_id = request.path_params["character_id"]
        if not manager.characters.get(character_id):
            return _error("Personaje no encontrado.", 404)
        if character_id in manager.active_characters():
            return _error("Este personaje tiene videos en producción.", 409)
        manager.characters.delete(character_id)
        return JSONResponse({"ok": True})

    async def suggest_characters(request: Request):
        """3 ideas de personaje (con su nicho) que no repitan nombre ni nicho de los existentes
        ni de las ideas ya mostradas."""
        payload = await _json(request)
        idioma, edad = payload.get("idioma") or "es", str(payload.get("edad") or "6-9")
        if idioma not in core.LANGUAGES or not core.AGE_RE.match(edad):
            return _error("Idioma o edad no válidos.")
        pista = " ".join(str(payload.get("pista") or "").split())[:160]
        existing = [f"{c.nombre} ({c.nicho})" for c in manager.characters.list()]
        shown = [" ".join(str(t).split()) for t in (payload.get("evitar") or [])][:30]
        try:
            ideas = await character_suggester(idioma, edad, pista, existing + shown, 5)
        except Exception as exc:  # noqa: BLE001 — credenciales, red o respuesta rara del modelo
            return _error(f"No se pudieron proponer personajes: {exc}", 502)
        taken = {core.topic_key(c.nombre, "") for c in manager.characters.list()}
        taken |= {core.topic_key(t.split(" (")[0], "") for t in shown}
        picked = []
        for idea in ideas:
            key = core.topic_key(idea["personaje"]["nombre"], "")
            if key in taken:
                continue
            taken.add(key)
            picked.append(idea)
            if len(picked) == 3:
                break
        if not picked:
            return _error("No salieron personajes nuevos. Prueba otra vez o cambia la pista.", 502)
        return JSONResponse({"personajes": picked})

    # ------------------------------------------------------------------ trabajos

    def _with_tiktok(jobs: list[dict]) -> list[dict]:
        published = publisher.published()
        for job in jobs:
            job["en_tiktok"] = job["slug"] in published
        return jobs

    async def list_jobs(request: Request):
        return JSONResponse(_with_tiktok(await asyncio.to_thread(manager.list)))

    async def create_job(request: Request):
        try:
            payload = await _json(request)
            job = manager.create(_request_from(payload), payload.get("personaje_id"))
        except (ValueError, TypeError) as exc:
            return _error(str(exc))
        return JSONResponse(job, status_code=201)

    async def get_job(request: Request):
        job = manager.get(request.path_params["job_id"])
        return JSONResponse(_with_tiktok([job])[0]) if job else _error("Trabajo no encontrado.", 404)

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

    # ------------------------------------------------------------------ publicación (Buffer + Drive)

    async def publishing_config(request: Request):
        return JSONResponse(publisher.config())

    async def save_publishing(request: Request):
        try:
            return JSONResponse(publisher.save_config(await _json(request)))
        except (PublishError, ValueError, OSError) as exc:
            return _error(str(exc))

    async def publishing_channels(request: Request):
        try:
            return JSONResponse(await publisher.channels())
        except PublishError as exc:
            return _error(str(exc), 502)

    async def publishing_test(request: Request):
        if request.method == "GET":
            return JSONResponse(publisher.state(TEST_SLUG))
        try:
            return JSONResponse(publisher.start(TEST_SLUG, None))
        except PublishError as exc:
            return _error(str(exc), 409)

    async def video_publish(request: Request):
        slug = request.path_params["slug"]
        try:
            out_dir = library.slug_dir(slug)
        except ValueError:
            return _error("Video no válido.", 400)
        if request.method == "GET":
            publish = library.read_json(out_dir / "publish.json")
            text = default_text(publish if isinstance(publish, dict) else {})
            return JSONResponse({**publisher.state(slug), "default_text": text,
                                 "default_units": text_units(text), "config": publisher.config()})
        try:
            payload = await _json(request)
            req = PublishRequest(text=str(payload.get("text") or ""), mode=str(payload.get("mode") or "queue"),
                                 due_at=payload.get("due_at") or None, ai_label=bool(payload.get("ai_label", True)),
                                 again=bool(payload.get("again")))
            return JSONResponse(publisher.start(slug, req))
        except (PublishError, ValueError) as exc:
            return _error(str(exc), 409 if "ya se envió" in str(exc) or "ya está publicado" in str(exc) else 400)

    async def video_mark_published(request: Request):
        slug = request.path_params["slug"]
        try:
            if request.method == "DELETE":
                return JSONResponse(publisher.unmark(slug, int(request.path_params.get("pub_id") or 0)))
            return JSONResponse(publisher.mark_manual(slug))
        except ValueError:
            return _error("Video no válido.", 400)
        except PublishError as exc:
            return _error(str(exc), 409)

    # ------------------------------------------------------------------ biblioteca

    async def list_videos(request: Request):
        videos = await asyncio.to_thread(library.list_videos)
        published = publisher.published()
        for video in videos:
            video["en_tiktok"] = video["slug"] in published
        return JSONResponse(videos)

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

    async def cover(request: Request):
        try:
            out_dir = library.slug_dir(request.path_params["slug"])
        except ValueError:
            return _error("Video no válido.", 400)
        image = await asyncio.to_thread(library.cover, out_dir)
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
        Route("/api/suggest", suggest, methods=["POST"]),
        Route("/api/characters", list_characters),
        Route("/api/characters", create_character, methods=["POST"]),
        Route("/api/characters/suggest", suggest_characters, methods=["POST"]),
        Route("/api/characters/{character_id}", get_character),
        Route("/api/characters/{character_id}", update_character, methods=["PUT"]),
        Route("/api/characters/{character_id}", delete_character, methods=["DELETE"]),
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
        Route("/api/videos/{slug}/publish", video_publish, methods=["GET", "POST"]),
        Route("/api/videos/{slug}/publish/manual", video_mark_published, methods=["POST"]),
        Route("/api/videos/{slug}/publish/{pub_id:int}", video_mark_published, methods=["DELETE"]),
        Route("/api/publishing", publishing_config),
        Route("/api/publishing", save_publishing, methods=["PUT"]),
        Route("/api/publishing/channels", publishing_channels),
        Route("/api/publishing/test", publishing_test, methods=["GET", "POST"]),
        Route("/poster/{slug}.jpg", poster),
        Route("/cover/{slug}.jpg", cover),
        Route("/files/{slug}/{path:path}", output_file),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
        Mount("/fonts", StaticFiles(directory=core.FONTS_DIR, check_dir=False), name="fonts"),
    ]
    token = token if token is not None else os.environ.get("PAPER_STAGE_TOKEN")
    middleware = [Middleware(TokenAuth, token=token)] if token else []
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.manager = manager
    app.state.publisher = publisher
    return app
