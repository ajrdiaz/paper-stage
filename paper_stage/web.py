"""Aplicación web de Radar Studio (Starlette + uvicorn, sin paso de compilación)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
from dataclasses import fields
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

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
from .dramas import DramaError, Dramas
from .eleven import ElevenError, ElevenLabs
from .radar import Radar, RadarError
from .review import review_video, set_discarded

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


SUGGESTIONS = 5  # temas por tanda en «Sugerir»


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _request_from(payload: dict) -> core.VideoRequest:
    # La serie llega como serie_id y la copia el gestor (prepare), nunca el cliente.
    allowed = {f.name for f in fields(core.VideoRequest)} - {"slug", "extra", "serie", "referencias", "ganchos_previos"}
    data = {k: v for k, v in payload.items() if k in allowed and v not in (None, "")}
    if "max_turns" in data:
        data["max_turns"] = int(data["max_turns"])
    if "max_budget_usd" in data:
        data["max_budget_usd"] = float(data["max_budget_usd"])
    if not data.get("tema") or not isinstance(data["tema"], str):
        raise ValueError("Escribe un tema.")
    if not isinstance(data.get("inspiracion", []), list):
        raise ValueError("Inspiración no válida.")
    return core.VideoRequest(**data)


def _serie_from(payload: dict) -> core.Serie:
    allowed = {f.name for f in fields(core.Serie)} - {"id"}
    try:
        return core.Serie(**{k: v for k, v in payload.items() if k in allowed})
    except TypeError:
        raise ValueError("Faltan datos de la serie: nombre, nicho, estilo, tono y paleta.")


async def _json(request: Request) -> dict:
    try:
        payload = await request.json()
    except ValueError:
        raise ValueError("El cuerpo de la petición no es JSON válido.")
    if not isinstance(payload, dict):
        raise ValueError("Se esperaba un objeto JSON.")
    return payload


class SameOriginGuard(BaseHTTPMiddleware):
    """Crear un trabajo lanza un agente con Bash: una web ajena abierta en este equipo no debe
    poder hacerlo. Sin token solo vale un Host local (DNS rebinding), y ninguna escritura
    entre sitios se acepta (un POST text/plain no pasa por el preflight de CORS)."""

    def __init__(self, app, local_only: bool):
        super().__init__(app)
        self.local_only = local_only

    async def dispatch(self, request: Request, call_next):
        if self.local_only and request.url.hostname not in LOCAL_HOSTS:
            return Response("Host no permitido.", status_code=403)
        origin = request.headers.get("origin")
        if origin and request.method not in ("GET", "HEAD") \
                and urlsplit(origin).netloc != request.headers.get("host"):
            return Response("Origen no permitido.", status_code=403)
        return await call_next(request)


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
               suggester: Callable | None = None,
               publisher: Publisher | None = None, radar: Radar | None = None,
               dramas: Dramas | None = None, eleven: ElevenLabs | None = None) -> Starlette:
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
    if radar is None:
        parts = {}
        if demo:
            from .demo import demo_radar_parts
            parts = demo_radar_parts()
        radar = Radar(manager.store.db, manager.data_dir, used_topics=manager.used_topics, **parts)
    if dramas is None:
        dramas = Dramas(manager.store.db, demo=demo)
    if eleven is None:
        eleven = ElevenLabs(manager.store.db, manager.data_dir, demo=demo)
    manager.source_texts = radar.transcripts
    if manager.reviewer is None:
        if demo:
            from .demo import review_demo
            manager.reviewer = review_demo
        else:
            manager.reviewer = review_video

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await manager.start()
        yield
        await radar.stop()
        await publisher.stop()
        await manager.stop()

    refreshing: dict[str, asyncio.Task | None] = {"task": None}

    def _with_tiktok(items: list[dict]) -> list[dict]:
        # Sin esperar a Buffer: las listas se piden cada pocos segundos, y el estado nuevo sale en la siguiente.
        task = refreshing["task"]
        if (not task or task.done()) and publisher.slugs_to_refresh():
            refreshing["task"] = asyncio.create_task(asyncio.to_thread(publisher.refresh_pending))
        published = publisher.published()
        for item in items:
            item["tiktok"] = published.get(item["slug"])
            item["en_tiktok"] = item["tiktok"] is not None
        return items

    # ------------------------------------------------------------------ configuración

    async def config(request: Request):
        return JSONResponse({
            "models": core.MODELS, "default_model": core.DEFAULT_MODEL,
            "durations": {k: v["etiqueta"] for k, v in core.DURATIONS.items()},
            "efforts": core.EFFORTS, "languages": core.LANGUAGES, "formats": core.FORMATS,
            "demo": demo, "concurrency": manager.concurrency, "default_cta": core.DEFAULT_CTA,
            "voices": core.VOICES, "narrativas": core.NARRATIVAS, "default_serie": manager.series.list()[0].id,
        })

    async def health(request: Request):
        return JSONResponse(await asyncio.to_thread(core.check_env))

    async def prompt_preview(request: Request):
        try:
            payload = await _json(request)
            req = manager.prepare(_request_from(payload), payload.get("serie_id"))
            return JSONResponse({"slug": req.slug, "prompt": core.render_prompt(req.variables()),
                                 "kickoff": core.kickoff_prompt(req)})
        except (ValueError, TypeError) as exc:
            return _error(str(exc))

    async def suggest(request: Request):
        """5 temas nuevos del nicho de la serie, primero los del Radar que encajen: ni ya producidos o en cola, ni
        entre los que ya se mostraron, ni el mismo tema con otras palabras."""
        payload = await _json(request)
        serie = manager.series.get(payload.get("serie_id")) if payload.get("serie_id") else manager.series.list()[0]
        if not serie:
            return _error("Serie no encontrada.", 404)
        idioma = payload.get("idioma") or serie.idioma
        if idioma not in core.LANGUAGES:
            return _error("Idioma no válido.")
        pista = " ".join(str(payload.get("pista") or "").split())[:120]
        shown = [" ".join(str(t).split()) for t in (payload.get("evitar") or [])][:60]
        used = await asyncio.to_thread(manager.used_topics, idioma, serie.id)
        taken = used + shown
        # Las ideas del Radar están en español; las ya hechas, mostradas o parecidas no se ofrecen.
        context = await asyncio.to_thread(radar.for_suggestions) if idioma == "es" else {}
        radar_ideas = {i["id"]: i for i in context.get("ideas", [])
                       if not any(core.same_topic(i["tema"], t) for t in taken)}
        context = {**context, "ideas": list(radar_ideas.values())[:12]}
        try:
            ideas = await suggester(serie, idioma, pista, taken, SUGGESTIONS + 3, radar=context)
        except Exception as exc:  # noqa: BLE001 — credenciales, red o respuesta rara del modelo
            return _error(f"No se pudieron sugerir temas: {exc}", 502)
        picked = []
        for idea in sorted(ideas, key=lambda i: i.get("radar", "") not in radar_ideas):  # las del Radar, primero
            source = radar_ideas.get(idea.get("radar", ""))
            if source:  # el tema exacto del Radar, con lo que trae para el guion
                notas = [source.get("gancho") and f"Gancho sugerido: {source['gancho']}",
                         source.get("cierre") and f"Cierre sugerido: {source['cierre']}",
                         source.get("verificar") and f"Verificar con fuentes: {source['verificar']}"]
                idea = {"tema": source["tema"], "gancho": source.get("gancho") or idea.get("gancho", ""), "origen": "radar",
                        "notas": "\n".join(n for n in notas if n), "fuentes": source.get("fuentes", [])}
            else:
                idea = {"tema": idea["tema"], "gancho": idea.get("gancho", ""), "origen": "claude"}
            if not 2 <= len(idea["tema"]) <= 200 or any(core.same_topic(idea["tema"], t) for t in taken):
                continue
            if await asyncio.to_thread(manager.is_used, idea["tema"], idioma, serie.id):
                continue
            taken.append(idea["tema"])
            picked.append(idea)
            if len(picked) == SUGGESTIONS:
                break
        if not picked:
            return _error("No salieron temas nuevos. Prueba otra vez o cambia la pista.", 502)
        return JSONResponse({"temas": picked})

    # ------------------------------------------------------------------ series

    def _serie_json(serie: core.Serie, videos: list[dict]) -> dict:
        mine = [v for v in videos if v["serie"] == serie.id]
        done = [v for v in mine if v["has_video"]]
        return {**serie.to_dict(), "hashtag": serie.hashtag, "videos": len(mine),
                "ultimo_video": done[0]["slug"] if done else None, "canal": publisher.channel_for(serie.id)}

    async def list_series(request: Request):
        videos = await asyncio.to_thread(library.list_videos)
        return JSONResponse([_serie_json(c, videos) for c in manager.series.list()])

    async def get_serie(request: Request):
        serie = manager.series.get(request.path_params["serie_id"])
        if not serie:
            return _error("Serie no encontrada.", 404)
        return JSONResponse(_serie_json(serie, await asyncio.to_thread(library.list_videos)))

    async def create_serie(request: Request):
        try:
            serie = manager.series.create(_serie_from(await _json(request)))
        except ValueError as exc:
            return _error(str(exc))
        return JSONResponse(_serie_json(serie, []), status_code=201)

    async def update_serie(request: Request):
        try:
            serie = manager.series.update(request.path_params["serie_id"],
                                                  _serie_from(await _json(request)))
        except KeyError:
            return _error("Serie no encontrada.", 404)
        except ValueError as exc:
            return _error(str(exc))
        return JSONResponse(_serie_json(serie, await asyncio.to_thread(library.list_videos)))

    async def delete_serie(request: Request):
        serie_id = request.path_params["serie_id"]
        if not manager.series.get(serie_id):
            return _error("Serie no encontrada.", 404)
        if serie_id in manager.active_series():
            return _error("Esta serie tiene videos en producción.", 409)
        if len(manager.series.list()) == 1:
            return _error("Es tu única serie: crea otra antes de borrarla.", 409)
        manager.series.delete(serie_id)
        publisher.set_channel(serie_id, "")
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------ trabajos

    async def list_jobs(request: Request):
        return JSONResponse(_with_tiktok(await asyncio.to_thread(manager.list)))

    async def create_job(request: Request):
        try:
            payload = await _json(request)
            job = manager.create(_request_from(payload), payload.get("serie_id"))
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

    async def publishing_schedule(request: Request):
        """Lo programado en Buffer, con su título y las correcciones importantes que aún tenga."""
        items = []
        for item in publisher.upcoming():
            out_dir = library.slug_dir(item["slug"])
            script = library.read_json(out_dir / "script.json")
            script = script if isinstance(script, dict) else {}
            items.append({**item, "titulo": str(script.get("titulo") or item["slug"]), "serie": library.script_serie(script),
                          "pendientes": library.serious_suggestions(out_dir)})
        return JSONResponse(items)

    async def publishing_channels(request: Request):
        try:
            return JSONResponse(await publisher.channels())
        except PublishError as exc:
            return _error(str(exc), 502)

    async def serie_channel(request: Request):
        """Asigna la cuenta de TikTok (canal de Buffer) de una serie; vacía, se la quita."""
        serie_id = request.path_params["serie_id"]
        if not manager.series.get(serie_id):
            return _error("Serie no encontrada.", 404)
        try:
            payload = await _json(request)
        except ValueError as exc:
            return _error(str(exc))
        channel_id = str(payload.get("channel_id") or "").strip()
        publisher.set_channel(serie_id, channel_id, str(payload.get("channel_name") or "").strip())
        return JSONResponse({"canal": publisher.channel_for(serie_id)})

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
            await asyncio.to_thread(publisher.refresh, slug)
            publish = library.read_json(out_dir / "publish.json")
            text = default_text(publish if isinstance(publish, dict) else {})
            serie, channel = publisher.video_channel(slug)
            return JSONResponse({**publisher.state(slug), "default_text": text, "serie": serie, "channel": channel,
                                 "default_units": text_units(text), "config": publisher.config(),
                                 "pendientes": library.serious_suggestions(library.slug_dir(slug))})
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

    # ------------------------------------------------------------------ minidramas (xAI)

    async def dramas_config(request: Request):
        return JSONResponse(dramas.config())

    async def save_dramas(request: Request):
        try:
            return JSONResponse(dramas.save_config(await _json(request)))
        except (DramaError, ValueError) as exc:
            return _error(str(exc))

    async def check_dramas(request: Request):
        try:
            await asyncio.to_thread(dramas.check)
        except DramaError as exc:
            return _error(str(exc), 409)
        return JSONResponse(dramas.config())

    # ------------------------------------------------------------------ voz con ElevenLabs

    async def eleven_config(request: Request):
        return JSONResponse(eleven.config())

    async def save_eleven(request: Request):
        try:
            return JSONResponse(eleven.save_config(await _json(request)))
        except (ElevenError, ValueError) as exc:
            return _error(str(exc))

    async def check_eleven(request: Request):
        try:
            await asyncio.to_thread(eleven.check)
        except ElevenError as exc:
            return _error(str(exc), 409)
        return JSONResponse(eleven.config())

    async def eleven_samples(request: Request):
        """Gasta créditos: la interfaz muestra antes el costo estimado y pide confirmación."""
        try:
            data = await _json(request)
            slug, model = str(data.get("slug", "")), str(data.get("model", ""))
            scene, voices = int(data.get("scene", 1)), [str(v) for v in data.get("voices") or []]
            if data.get("estimate"):
                return JSONResponse({"credits": eleven.estimate(slug, scene, voices, model)})
            result = await asyncio.to_thread(eleven.make_samples, slug, scene, voices, model)
        except (ValueError, TypeError) as exc:
            return _error(str(exc) or "Datos no válidos.")
        except ElevenError as exc:
            return _error(str(exc), 409)
        return JSONResponse({**eleven.config(), "result": result})

    async def eleven_sample_file(request: Request):
        p = request.path_params
        path = eleven.sample_path(p["slug"], p["scene"], p["model"], p["voice"])
        return FileResponse(path, media_type="audio/wav") if path else _error("Muestra no encontrada.", 404)

    # ------------------------------------------------------------------ radar

    async def radar_overview(request: Request):
        return JSONResponse(radar.overview())

    def _radar_action(action):
        """Las acciones del radar responden con el estado completo, para repintar la vista."""
        async def endpoint(request: Request):
            try:
                await action(request)
            except KeyError:
                return _error("No encontrado.", 404)
            except RadarError as exc:
                return _error(str(exc), 409)
            except ValueError as exc:
                return _error(str(exc))
            return JSONResponse(radar.overview())
        return endpoint

    async def _radar_config(request: Request):
        radar.save_config(await _json(request))

    async def _radar_add(request: Request):
        radar.add_account((await _json(request)).get("handle"))

    async def _radar_remove(request: Request):
        radar.remove_account(request.path_params["handle"])

    async def _radar_scan(request: Request):
        handles = (await _json(request)).get("handles")
        radar.start_scan([str(h) for h in handles] if isinstance(handles, list) else None)

    async def _radar_analyze(request: Request):
        radar.start_analysis(request.path_params["video_id"])

    async def _radar_niches(request: Request):
        body = await _json(request)
        radar.start_niches(body.get("tema"), body.get("mercado"))

    async def _radar_search(request: Request):
        radar.start_account_search(request.path_params["run_id"], request.path_params["index"])

    async def _radar_choose(request: Request):
        radar.choose_niche(request.path_params["run_id"], request.path_params["index"])

    async def _radar_ideas(request: Request):
        radar.start_ideas()

    async def _radar_discard(request: Request):
        radar.discard_idea(request.path_params["idea_id"])

    async def radar_cover(request: Request):
        path = radar.cover_path(request.path_params["video_id"])
        return FileResponse(path) if path else _error("Sin portada.", 404)

    # ------------------------------------------------------------------ biblioteca

    async def list_videos(request: Request):
        return JSONResponse(_with_tiktok(await asyncio.to_thread(library.list_videos)))

    async def get_video(request: Request):
        slug = request.path_params["slug"]
        try:
            data = await asyncio.to_thread(library.detail, slug)
        except ValueError:
            return _error("Video no válido.", 400)
        if not data:
            return _error("Video no encontrado.", 404)
        data["jobs"] = [j for j in manager.store.list() if j["slug"] == slug]
        data["revisando"], data["revision_error"] = manager.reviewing(slug), manager.review_errors.get(slug)
        return JSONResponse(data)

    async def video_review(request: Request):
        try:
            manager.start_review(request.path_params["slug"])
        except ValueError as exc:
            return _error(str(exc), 409)
        return JSONResponse({"ok": True})

    async def discard_suggestion(request: Request):
        try:
            payload = await _json(request)
            out_dir = library.slug_dir(request.path_params["slug"])
            await asyncio.to_thread(set_discarded, out_dir, request.path_params["suggestion"], bool(payload.get("descartada", True)))
        except ValueError as exc:
            return _error(str(exc), 404)
        return JSONResponse({"ok": True})

    async def video_corrections(request: Request):
        try:
            payload = await _json(request)
            texts = payload.get("correcciones")
            if not isinstance(texts, list):
                raise ValueError("Elige al menos una corrección.")
            job = manager.create_correction(request.path_params["slug"], [str(t) for t in texts])
        except ValueError as exc:
            return _error(str(exc), 409)
        return JSONResponse(job, status_code=201)

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
        Route("/api/series", list_series),
        Route("/api/series", create_serie, methods=["POST"]),
        Route("/api/series/{serie_id}", get_serie),
        Route("/api/series/{serie_id}", update_serie, methods=["PUT"]),
        Route("/api/series/{serie_id}", delete_serie, methods=["DELETE"]),
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
        Route("/api/videos/{slug}/review", video_review, methods=["POST"]),
        Route("/api/videos/{slug}/corrections", video_corrections, methods=["POST"]),
        Route("/api/videos/{slug}/review/{suggestion}/discard", discard_suggestion, methods=["POST"]),
        Route("/api/videos/{slug}/publish", video_publish, methods=["GET", "POST"]),
        Route("/api/videos/{slug}/publish/manual", video_mark_published, methods=["POST"]),
        Route("/api/videos/{slug}/publish/{pub_id:int}", video_mark_published, methods=["DELETE"]),
        Route("/api/dramas", dramas_config),
        Route("/api/dramas", save_dramas, methods=["PUT"]),
        Route("/api/dramas/check", check_dramas, methods=["POST"]),
        Route("/api/voz/elevenlabs", eleven_config),
        Route("/api/voz/elevenlabs", save_eleven, methods=["PUT"]),
        Route("/api/voz/elevenlabs/check", check_eleven, methods=["POST"]),
        Route("/api/voz/elevenlabs/samples", eleven_samples, methods=["POST"]),
        Route("/voces/muestras/{slug}/{scene}/{model}/{voice}.wav", eleven_sample_file),
        Route("/api/publishing", publishing_config),
        Route("/api/publishing", save_publishing, methods=["PUT"]),
        Route("/api/publishing/channels", publishing_channels),
        Route("/api/publishing/channels/{serie_id}", serie_channel, methods=["PUT"]),
        Route("/api/publishing/test", publishing_test, methods=["GET", "POST"]),
        Route("/api/publishing/schedule", publishing_schedule),
        Route("/api/radar", radar_overview),
        Route("/api/radar/config", _radar_action(_radar_config), methods=["PUT"]),
        Route("/api/radar/accounts", _radar_action(_radar_add), methods=["POST"]),
        Route("/api/radar/accounts/{handle}", _radar_action(_radar_remove), methods=["DELETE"]),
        Route("/api/radar/scan", _radar_action(_radar_scan), methods=["POST"]),
        Route("/api/radar/videos/{video_id}/analyze", _radar_action(_radar_analyze), methods=["POST"]),
        Route("/api/radar/niches", _radar_action(_radar_niches), methods=["POST"]),
        Route("/api/radar/niches/{run_id}/{index:int}/accounts", _radar_action(_radar_search), methods=["POST"]),
        Route("/api/radar/niches/{run_id}/{index:int}/choose", _radar_action(_radar_choose), methods=["POST"]),
        Route("/api/radar/ideas", _radar_action(_radar_ideas), methods=["POST"]),
        Route("/api/radar/ideas/{idea_id}", _radar_action(_radar_discard), methods=["DELETE"]),
        Route("/radar/cover/{video_id}.jpg", radar_cover),
        Route("/poster/{slug}.jpg", poster),
        Route("/cover/{slug}.jpg", cover),
        Route("/files/{slug}/{path:path}", output_file),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
        Mount("/fonts", StaticFiles(directory=core.FONTS_DIR, check_dir=False), name="fonts"),
    ]
    token = token if token is not None else os.environ.get("PAPER_STAGE_TOKEN")
    middleware = [Middleware(SameOriginGuard, local_only=not token)]
    if token:
        middleware.append(Middleware(TokenAuth, token=token))
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.manager = manager
    app.state.publisher = publisher
    app.state.radar = radar
    return app
