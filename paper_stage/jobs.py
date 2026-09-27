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
        self.runner = runner or core.run_agent
        self.concurrency = max(1, concurrency)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.workers: list[asyncio.Task] = []
        self.running: dict[str, asyncio.Task] = {}
        self.cancel_requested: set[str] = set()
        self.subscribers: dict[str, set[asyncio.Queue]] = {}
        self.seq: dict[str, int] = {}
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

    def used_topics(self, idioma: str | None = None) -> list[str]:
        """Temas ya producidos o en cola: los de los trabajos y los de output/ hechos desde
        la terminal (de estos solo se conoce el título)."""
        topics, slugs = [], set()
        for slug, req in self.store.requests():
            slugs.add(slug)
            if idioma is None or req.get("idioma") == idioma:
                topics.append(req["tema"])
        for video in library.list_videos():
            if video["slug"] not in slugs and (idioma is None or video.get("idioma") in (None, idioma)):
                topics.append(video["title"])
        return list(dict.fromkeys(topics))

    def is_used(self, tema: str, idioma: str) -> bool:
        key = core.topic_key(tema, idioma)
        requests = self.store.requests()
        if any(core.topic_key(req["tema"], req.get("idioma", "es")) == key for _, req in requests):
            return True
        # Hecho desde la terminal: su carpeta es el slug del tema.
        slug = core.slugify(f"{tema}-{idioma}")
        return slug not in {s for s, _ in requests} and (core.OUTPUT_DIR / slug / "script.json").exists()

    def create(self, req: core.VideoRequest) -> dict:
        req.validate()
        if self.is_used(req.tema, req.idioma):
            raise ValueError(f"Ya hay un video sobre «{req.tema}» en este idioma. "
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
            check = await asyncio.to_thread(library.verify, req.out_dir)
            if check and not check["ok"]:
                failed = [f"{c['label']} ({c['detail']})" for c in check["checks"] if not c["ok"]]
                self.emit(job_id, {"kind": "text",
                                   "text": "Revisión automática de la app, no cumple: " + "; ".join(failed)})
            self._set_status(job_id, "done", finished_at=time.time())
        else:
            if not result:
                error = "El agente terminó sin enviar un resultado."
            elif not result.get("ok"):
                error = f"El agente terminó con error ({result.get('subtype')})."
            else:
                error = "El agente terminó sin generar final.mp4."
            self._set_status(job_id, "failed", finished_at=time.time(), error=error)
