"""Cola de trabajos: persistencia en SQLite, ejecución, cancelación, reanudación y eventos en vivo."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import AsyncIterator, Callable

from . import core, library

TERMINAL = {"done", "failed", "cancelled", "interrupted"}
RESUMABLE = {"failed", "cancelled", "interrupted"}

Runner = Callable[..., AsyncIterator[dict]]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    request TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    cost_usd REAL NOT NULL DEFAULT 0,
    turns INTEGER NOT NULL DEFAULT 0,
    session_id TEXT,
    error TEXT,
    result TEXT,
    resume INTEGER NOT NULL DEFAULT 0
)
"""


SERIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""


class SerieStore:
    """Series, en la misma base de datos que los trabajos. Si no hay ninguna, crea la de
    partida: el estudio siempre tiene al menos una con la que producir."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.execute(SERIES_SCHEMA)
        if not self.db.execute("SELECT 1 FROM series LIMIT 1").fetchone():
            self._save(core.Serie.from_dict(core.DEFAULT_SERIE.to_dict()), insert=True)
        self.db.commit()

    def list(self) -> list[core.Serie]:
        rows = self.db.execute("SELECT data FROM series ORDER BY created_at")
        return [core.Serie.from_dict(json.loads(data)) for (data,) in rows]

    def get(self, serie_id: str) -> core.Serie | None:
        row = self.db.execute("SELECT data FROM series WHERE id = ?", [serie_id]).fetchone()
        return core.Serie.from_dict(json.loads(row[0])) if row else None

    def create(self, serie: core.Serie) -> core.Serie:
        serie.validate()
        taken = {c.id for c in self.list()}
        base = core.slugify(serie.nombre)[:40].strip("-") or "serie"
        serie.id, n = base, 2
        while serie.id in taken:
            serie.id, n = f"{base}-{n}", n + 1
        self._save(serie, insert=True)
        self.db.commit()
        return serie

    def update(self, serie_id: str, serie: core.Serie) -> core.Serie:
        if not self.get(serie_id):
            raise KeyError(serie_id)
        serie.id = serie_id
        serie.validate()
        self._save(serie, insert=False)
        self.db.commit()
        return serie

    def delete(self, serie_id: str) -> None:
        self.db.execute("DELETE FROM series WHERE id = ?", [serie_id])
        self.db.commit()

    def _save(self, serie: core.Serie, insert: bool) -> None:
        data, now = json.dumps(serie.to_dict(), ensure_ascii=False), time.time()
        if insert:
            self.db.execute("INSERT INTO series (id, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
                            [serie.id, data, now, now])
        else:
            self.db.execute("UPDATE series SET data = ?, updated_at = ? WHERE id = ?",
                            [data, now, serie.id])


def request_serie(req: dict) -> str:
    """ID de la serie de una petición guardada."""
    return (req.get("serie") or {}).get("id") or core.DEFAULT_SERIE.id


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.db.commit()

    def insert(self, job: dict) -> None:
        cols = ", ".join(job)
        marks = ", ".join("?" for _ in job)
        self.db.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", list(job.values()))
        self.db.commit()

    def update(self, job_id: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE jobs SET {sets} WHERE id = ?", [*fields.values(), job_id])
        self.db.commit()

    def get(self, job_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE id = ?", [job_id]).fetchone()
        return self._row(row) if row else None

    def requests(self) -> list[tuple[str, dict]]:
        """(slug, petición) de todos los trabajos, del más antiguo al más reciente."""
        rows = self.db.execute("SELECT slug, request FROM jobs ORDER BY created_at")
        return [(slug, json.loads(request)) for slug, request in rows]

    def list(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", [limit])
        return [self._row(r) for r in rows]

    def by_status(self, *statuses: str) -> list[dict]:
        marks = ", ".join("?" for _ in statuses)
        rows = self.db.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY created_at", statuses
        )
        return [self._row(r) for r in rows]

    def delete(self, job_id: str) -> None:
        self.db.execute("DELETE FROM jobs WHERE id = ?", [job_id])
        self.db.commit()

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        job = dict(row)
        job["request"] = json.loads(job["request"])
        job["resume"] = bool(job["resume"])
        return job


class JobManager:
    def __init__(self, data_dir: Path, runner: Runner | None = None, concurrency: int = 1):
        self.data_dir = data_dir
        self.events_dir = data_dir / "jobs"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.store = JobStore(data_dir / "paper_stage.db")
        self.series = SerieStore(self.store.db)
        self.runner = runner or core.run_agent
        self.concurrency = max(1, concurrency)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.workers: list[asyncio.Task] = []
        self.running: dict[str, asyncio.Task] = {}
        self.cancel_requested: set[str] = set()
        self.subscribers: dict[str, set[asyncio.Queue]] = {}
        self.seq: dict[str, int] = {}
        # Transcripciones de los videos que inspiraron un guion (las pone la web, desde el Radar).
        self.source_texts: Callable[[list[str]], list[str]] = lambda ids: []
        # Revisión de audio y video de un video terminado (la pone la web).
        self.reviewer: Callable | None = None
        self.reviews: dict[str, asyncio.Task] = {}
        self.review_errors: dict[str, str] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stopping = False

    # ------------------------------------------------------------------ ciclo de vida

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        for job in self.store.by_status("running"):
            self._set_status(job["id"], "interrupted",
                             error="El servidor se detuvo mientras el trabajo corría.")
        for job in self.store.by_status("queued"):
            self.queue.put_nowait(job["id"])
        self.workers = [asyncio.create_task(self._worker()) for _ in range(self.concurrency)]

    async def stop(self) -> None:
        self.stopping = True
        for task in self.reviews.values():
            task.cancel()
        tasks = [*self.running.values(), *self.workers]
        for job_id, task in self.running.items():
            if job_id not in self.cancel_requested:  # no interrumpir un cierre ya en curso
                task.cancel()
        for task in self.workers:
            task.cancel()
        if tasks:
            # Si un trabajo tarda en cerrar el proceso del agente, no bloquea el apagado.
            await asyncio.wait(tasks, timeout=20)

    # ------------------------------------------------------------------ API pública

    # Los temas no se repiten dentro de una serie; otra, con su propio nicho, sí puede tratar el mismo.

    def used_topics(self, idioma: str | None = None, serie: str | None = None) -> list[str]:
        """Temas ya producidos o en cola: los de los trabajos y los de output/ hechos desde
        la terminal (de estos solo se conoce el título)."""
        topics, slugs = [], set()
        for slug, req in self.store.requests():
            slugs.add(slug)
            if (idioma is None or req.get("idioma") == idioma) and \
                    (serie is None or request_serie(req) == serie):
                topics.append(req["tema"])
        for video in library.list_videos():
            if video["slug"] not in slugs and (idioma is None or video.get("idioma") in (None, idioma)) \
                    and (serie is None or video["serie"] == serie):
                topics.append(video["title"])
        return list(dict.fromkeys(topics))

    def is_used(self, tema: str, idioma: str, serie: str) -> bool:
        key = core.topic_key(tema, idioma)
        requests = self.store.requests()
        if any(core.topic_key(req["tema"], req.get("idioma", "es")) == key
               and request_serie(req) == serie for _, req in requests):
            return True
        # Hecho desde la terminal: su carpeta es el slug del tema.
        slug = core.slugify(f"{tema}-{idioma}")
        script = library.read_json(core.OUTPUT_DIR / slug / "script.json")
        return (slug not in {s for s, _ in requests} and isinstance(script, dict)
                and library.script_serie(script) == serie)

    def prepare(self, req: core.VideoRequest, serie: str | None = None) -> core.VideoRequest:
        """Copia la serie en la petición (sin indicar, la primera), busca sus videos anteriores y la valida."""
        serie = self.series.get(serie) if serie else self.series.list()[0]
        if not serie:
            raise ValueError(f"No existe la serie «{serie}».")
        req.serie = serie.to_dict()
        req.referencias = library.videos_of(serie.id)
        req.ganchos_previos = library.hooks_of(serie.id)
        req.validate()
        return req

    def create(self, req: core.VideoRequest, serie: str | None = None) -> dict:
        self.prepare(req, serie)
        if self.is_used(req.tema, req.idioma, req.ficha.id):
            raise ValueError(f"«{req.ficha.nombre}» ya tiene un video sobre «{req.tema}» en este idioma. "
                             "Los temas no se repiten: elige uno distinto.")
        req.slug = self._unique_slug(req.slug)
        job_id = uuid.uuid4().hex[:10]
        self.store.insert({
            "id": job_id,
            "slug": req.slug,
            "request": json.dumps(asdict(req), ensure_ascii=False),
            "status": "queued",
            "created_at": time.time(),
        })
        self.emit(job_id, {"kind": "status", "status": "queued"})
        self.queue.put_nowait(job_id)
        return self.get(job_id)

    def create_correction(self, slug: str, correcciones: list[str]) -> dict:
        """Un trabajo nuevo que corrige un video ya hecho, en su misma carpeta: no pasa por la
        comprobación de temas repetidos porque no es un tema nuevo."""
        out_dir = library.slug_dir(slug)
        if not (out_dir / "final.mp4").exists():
            raise ValueError("Este video todavía no tiene final.mp4.")
        if slug in self.active_slugs():
            raise ValueError("Ya hay un trabajo en curso para este video.")
        previous = next((j["request"] for j in self.store.list() if j["slug"] == slug), None)
        if previous is None:  # hecho desde la terminal
            script = library.read_json(out_dir / "script.json")
            serie = self.series.get(library.script_serie(script)) or self.series.list()[0]
            previous = {"tema": library.summary(out_dir)["title"], "serie": serie.to_dict(),
                        "idioma": (script or {}).get("idioma") or serie.idioma, "formato": (script or {}).get("formato") or "vertical"}
        req = core.VideoRequest(**{**previous, "correcciones": correcciones, "slug": slug})
        req.validate()
        if not req.correcciones:
            raise ValueError("Elige al menos una corrección.")
        job_id = uuid.uuid4().hex[:10]
        self.store.insert({"id": job_id, "slug": slug, "request": json.dumps(asdict(req), ensure_ascii=False),
                           "status": "queued", "created_at": time.time()})
        self.emit(job_id, {"kind": "status", "status": "queued"})
        self.queue.put_nowait(job_id)
        return self.get(job_id)

    def start_review(self, slug: str) -> None:
        """Revisa audio y video en segundo plano: el video ya está listo y esto tarda unos minutos."""
        out_dir = library.slug_dir(slug)
        if not self.reviewer or not (out_dir / "final.mp4").exists():
            raise ValueError("Este video todavía no se puede revisar.")
        if slug in self.reviews and not self.reviews[slug].done():
            raise ValueError("Ya se está revisando este video.")
        self.review_errors.pop(slug, None)
        previous = next((j["request"] for j in self.store.list() if j["slug"] == slug), {})
        self.reviews[slug] = asyncio.create_task(self._review(slug, out_dir, previous.get("serie")))

    async def _review(self, slug: str, out_dir: Path, serie: dict | None) -> None:
        try:
            await self.reviewer(out_dir, serie)
        except Exception as exc:  # noqa: BLE001 — un fallo aquí no debe afectar al video terminado
            self.review_errors[slug] = f"{type(exc).__name__}: {exc}"[:300]

    def reviewing(self, slug: str) -> bool:
        return slug in self.reviews and not self.reviews[slug].done()

    def get(self, job_id: str) -> dict | None:
        job = self.store.get(job_id)
        if job:
            job["steps"] = library.steps_for(core.OUTPUT_DIR / job["slug"])
            job["queue_position"] = self._queue_position(job_id)
        return job

    def list(self) -> list[dict]:
        jobs = self.store.list()
        for job in jobs:
            steps = library.steps_for(core.OUTPUT_DIR / job["slug"])
            job["steps_done"] = sum(s["done"] for s in steps)
            job["steps_total"] = len(steps)
            job["queue_position"] = self._queue_position(job["id"])
        return jobs

    def cancel(self, job_id: str) -> dict:
        job = self._require(job_id)
        if job["status"] == "queued":
            self._set_status(job_id, "cancelled", finished_at=time.time())
        elif job["status"] == "running" and job_id in self.running:
            self.cancel_requested.add(job_id)
            self.running[job_id].cancel()
        else:
            raise ValueError("Solo se pueden cancelar trabajos en cola o en curso.")
        return self.get(job_id)

    def resume(self, job_id: str) -> dict:
        job = self._require(job_id)
        if job["status"] not in RESUMABLE:
            raise ValueError("Solo se pueden reanudar trabajos fallidos, cancelados o interrumpidos.")
        self._set_status(job_id, "queued", resume=1, error=None, finished_at=None)
        self.queue.put_nowait(job_id)
        return self.get(job_id)

    def delete(self, job_id: str) -> None:
        job = self._require(job_id)
        if job["status"] in ("queued", "running"):
            raise ValueError("Cancela el trabajo antes de borrarlo.")
        self.store.delete(job_id)
        (self.events_dir / f"{job_id}.jsonl").unlink(missing_ok=True)

    def active_slugs(self) -> set[str]:
        return {j["slug"] for j in self.store.by_status("queued", "running")}

    def active_series(self) -> set[str]:
        return {request_serie(j["request"]) for j in self.store.by_status("queued", "running")}

    # ------------------------------------------------------------------ eventos

    def emit(self, job_id: str, event: dict) -> dict:
        if job_id not in self.seq:
            self.seq[job_id] = len(self.events(job_id))
        self.seq[job_id] += 1
        event = {"seq": self.seq[job_id], "ts": time.time(), **event}
        with (self.events_dir / f"{job_id}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        for q in self.subscribers.get(job_id, ()):
            q.put_nowait(event)
        return event

    def events(self, job_id: str) -> list[dict]:
        path = self.events_dir / f"{job_id}.jsonl"
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.setdefault(job_id, set()).add(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        self.subscribers.get(job_id, set()).discard(q)

    # ------------------------------------------------------------------ internos

    def _require(self, job_id: str) -> dict:
        job = self.store.get(job_id)
        if not job:
            raise KeyError(job_id)
        return job

    def _queue_position(self, job_id: str) -> int | None:
        queued = [j["id"] for j in self.store.by_status("queued")]
        return queued.index(job_id) + 1 if job_id in queued else None

    def _unique_slug(self, slug: str) -> str:
        taken = self.active_slugs()
        candidate, n = slug, 2
        while candidate in taken or (core.OUTPUT_DIR / candidate).exists():
            candidate, n = f"{slug}-{n}", n + 1
        return candidate

    def _set_status(self, job_id: str, status: str, **fields) -> None:
        self.store.update(job_id, status=status, **fields)
        job = self.store.get(job_id)
        self.emit(job_id, {"kind": "status", "status": status, "error": job and job["error"]})

    async def _worker(self) -> None:
        while not self.stopping:
            job_id = await self.queue.get()
            job = self.store.get(job_id)
            if not job or job["status"] != "queued":
                continue
            task = asyncio.create_task(self._run(job))
            self.running[job_id] = task
            try:
                # shield: si se cancela el worker (apagado), stop() decide qué hacer con el trabajo.
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if self.stopping:
                    raise
            finally:
                self.running.pop(job_id, None)

    async def _watch_steps(self, job_id: str, out_dir: Path) -> None:
        last = None
        while True:
            steps = library.steps_for(out_dir)
            if steps != last:
                self.emit(job_id, {"kind": "steps", "steps": steps})
                last = steps
            await asyncio.sleep(3)

    def _stderr(self, job_id: str) -> Callable[[str], None]:
        def handler(line: str) -> None:
            line = line.strip()
            if line and self.loop:
                self.loop.call_soon_threadsafe(
                    self.emit, job_id, {"kind": "stderr", "text": line[:400]}
                )
        return handler

    async def _run(self, job: dict) -> None:
        job_id = job["id"]
        req = core.VideoRequest(**job["request"])
        resume_session = job["session_id"] if job["resume"] else None
        self._set_status(job_id, "running", started_at=time.time())
        if job["resume"]:
            self.emit(job_id, {"kind": "text", "text": "Reanudando el trabajo…"})
        watcher = asyncio.create_task(self._watch_steps(job_id, req.out_dir))
        cost = job["cost_usd"] or 0.0
        result: dict | None = None
        stream = self.runner(
            req,
            resume_session=resume_session,
            continuing=job["resume"],
            on_stderr=self._stderr(job_id),
        )
        try:
            try:
                async for event in stream:
                    if event["kind"] == "session":
                        self.store.update(job_id, session_id=event["session_id"])
                    elif event["kind"] == "result":
                        result = event
                        cost += event.get("cost_usd") or 0.0
                        self.store.update(
                            job_id,
                            cost_usd=cost,
                            turns=(job["turns"] or 0) + (event.get("turns") or 0),
                            session_id=event.get("session_id") or job["session_id"],
                            result=event.get("text") or None,
                        )
                    self.emit(job_id, event)
            finally:
                # Cierra el generador aunque el bucle se interrumpa, para que el
                # SDK termine el proceso de Claude Code al cancelar.
                await stream.aclose()
        except asyncio.CancelledError:
            by_user = job_id in self.cancel_requested
            self.cancel_requested.discard(job_id)
            self._set_status(
                job_id, "cancelled" if by_user else "interrupted", finished_at=time.time(),
                error=None if by_user else "El servidor se detuvo mientras el trabajo corría.",
            )
            if not by_user or self.stopping:
                raise
            return
        except Exception as exc:  # noqa: BLE001 — cualquier fallo del agente termina el trabajo
            self._set_status(job_id, "failed", finished_at=time.time(),
                             error=f"{type(exc).__name__}: {exc}")
            return
        finally:
            watcher.cancel()
            self.emit(job_id, {"kind": "steps", "steps": library.steps_for(req.out_dir)})

        has_video = (req.out_dir / "final.mp4").exists()
        if result and result.get("ok") and has_video:
            await asyncio.to_thread(library.cover, req.out_dir)
            library.poster(req.out_dir)
            # El agente ya terminó: el texto de terceros no pasa por sus manos.
            library.write_similarity(req.out_dir, self.source_texts(req.inspiracion))
            check = await asyncio.to_thread(library.verify, req.out_dir)
            if check and not check["ok"]:
                failed = [f"{c['label']} ({c['detail']})" for c in check["checks"] if not c["ok"]]
                self.emit(job_id, {"kind": "text",
                                   "text": "Revisión automática de la app, no cumple: " + "; ".join(failed)})
            self._set_status(job_id, "done", finished_at=time.time())
            if self.reviewer and not self.reviewing(req.slug):
                self.start_review(req.slug)
        else:
            if not result:
                error = "El agente terminó sin enviar un resultado."
            elif not result.get("ok"):
                error = f"El agente terminó con error ({result.get('subtype')})."
            else:
                error = "El agente terminó sin generar final.mp4."
            self._set_status(job_id, "failed", finished_at=time.time(), error=error)
