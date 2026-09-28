"""Publicación en TikTok vía Buffer, con el video alojado en Google Drive.

Buffer no acepta archivos: descarga el video de un enlace público cuando llega la hora de
publicar. Drive para escritorio sincroniza una carpeta local; si esa carpeta está compartida
como «Cualquier persona con el enlace», cada video que copiamos ahí queda en una URL de
descarga directa que Buffer sí puede leer. Por eso el video debe seguir en Drive hasta que
se publique.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from . import library

BUFFER_API = "https://api.buffer.com"
# Con un video adjunto, TikTok admite 2200 unidades UTF-16 de texto (guía de Buffer).
MAX_TIKTOK_TEXT = 2200
DRIVE_FOLDER_NAME = "Teatrito de Papel - publicar"
# Drive para escritorio guarda el ID de cada archivo en un atributo extendido; el nombre
# cambió entre versiones (Drive File Stream usaba user.drive.id).
DRIVE_ID_ATTRS = ("com.google.drivefs.item-id#S", "user.drive.id")
TEST_SLUG = "__prueba__"
MODES = {"queue": "addToQueue", "now": "shareNow", "schedule": "customScheduled"}
MANUAL = "manual"  # subido a TikTok a mano, fuera de la app: solo se registra para no repetirlo

# (método, url, cabeceras, cuerpo) → (estado HTTP, cabeceras, primeros bytes del cuerpo)
Http = Callable[[str, str, dict, bytes | None], tuple[int, dict, bytes]]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    channel_name TEXT,
    post_id TEXT,
    mode TEXT NOT NULL,
    due_at TEXT,
    url TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class PublishError(Exception):
    """Error que se le puede mostrar tal cual al usuario."""


def http_request(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return res.status, dict(res.headers), res.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read(64 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PublishError(f"No hay conexión con {url.split('/')[2]}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Google Drive
# --------------------------------------------------------------------------- #

def drive_roots() -> list[Path]:
    """Carpetas «Mi unidad» de las cuentas de Drive para escritorio de este Mac."""
    base = Path.home() / "Library" / "CloudStorage"
    return sorted(p for account in base.glob("GoogleDrive-*") for p in account.iterdir()
                  if p.is_dir() and p.name in ("My Drive", "Mi unidad"))


def drive_file_id(path: Path) -> str | None:
    """ID de Drive de un archivo sincronizado, o None si Drive aún no lo registró."""
    for attr in DRIVE_ID_ATTRS:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            out = subprocess.run(["xattr", "-p", attr, str(path)], capture_output=True,
                                 text=True, timeout=10)
            value = out.stdout.strip()
            # Antes de subirlo, Drive puede usar un ID provisional «local-…».
            if out.returncode == 0 and value and not value.startswith("local"):
                return value
    return None


def public_url(file_id: str) -> str:
    # Descarga directa, sin la página de vista previa ni redirecciones (Buffer no las sigue);
    # confirm=t evita el aviso de «no se pudo analizar en busca de virus» de los archivos grandes.
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"


def check_video_url(url: str, http: Http) -> tuple[bool, str]:
    """¿La URL sirve un MP4 a cualquiera, sin iniciar sesión? Lee solo los primeros bytes."""
    status, headers, body = http("GET", url, {"Range": "bytes=0-1023"}, None)
    kind = {k.lower(): v for k, v in headers.items()}.get("content-type", "")
    if status in (200, 206) and body[4:8] == b"ftyp":
        return True, "el enlace público sirve el video"
    if "html" in kind or body.lstrip()[:1] == b"<":
        return False, ("Drive responde con una página, no con el video: comparte la carpeta como "
                       "«Cualquier persona con el enlace» o espera a que termine de subirlo.")
    return False, f"Drive respondió {status} ({kind or 'sin tipo'})"


# --------------------------------------------------------------------------- #
# Buffer
# --------------------------------------------------------------------------- #

CHANNELS_QUERY = """
query { account { organizations { id name } } }
"""
ORG_CHANNELS_QUERY = """
query Channels($org: OrganizationId!) {
  channels(input: { organizationId: $org }) { id name displayName service }
}
"""
CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message }
  }
}
"""


def buffer_query(key: str, query: str, variables: dict | None, http: Http) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    status, _, raw = http("POST", BUFFER_API, {"Content-Type": "application/json",
                                                "Authorization": f"Bearer {key}"}, body)
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise PublishError(f"Buffer respondió algo que no es JSON (HTTP {status}).")
    errors = data.get("errors") or []
    if errors:
        code = (errors[0].get("extensions") or {}).get("code", "")
        if code == "UNAUTHORIZED" or status == 401:
            raise PublishError("Buffer rechazó la clave de API. Revísala en Sistema.")
        raise PublishError(f"Buffer: {errors[0].get('message')} {f'({code})' if code else ''}".strip())
    if status == 401:
        raise PublishError("Buffer rechazó la clave de API. Revísala en Sistema.")
    return data.get("data") or {}


def buffer_channels(key: str, http: Http) -> list[dict]:
    orgs = (buffer_query(key, CHANNELS_QUERY, None, http).get("account") or {}).get("organizations") or []
    channels = []
    for org in orgs:
        for ch in buffer_query(key, ORG_CHANNELS_QUERY, {"org": org["id"]}, http).get("channels") or []:
            channels.append({"id": ch["id"], "service": ch.get("service"), "organizacion": org.get("name"),
                             "nombre": ch.get("displayName") or ch.get("name") or ch["id"]})
    return channels


def buffer_create_post(key: str, post: dict, http: Http) -> dict:
    result = buffer_query(key, CREATE_POST, {"input": post}, http).get("createPost") or {}
    if "post" not in result:
        raise PublishError(f"Buffer no creó la publicación: {result.get('message') or 'sin detalle'}")
    return result["post"]


def text_units(text: str) -> int:
    """Largo como lo cuenta Buffer: unidades UTF-16 (un emoji cuenta 2)."""
    return len(text.encode("utf-16-le")) // 2


def default_text(publish: dict) -> str:
    """Descripción corta y hashtags de publish.json, como texto de la publicación."""
    parts = [str(publish.get("descripcion_corta") or publish.get("descripcion") or "").strip(),
             " ".join(str(h) for h in (publish.get("hashtags") or [])[:library.MAX_HASHTAGS])]
    return "\n\n".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# Orquestación
# --------------------------------------------------------------------------- #

@dataclass
class PublishRequest:
    text: str
    mode: str = "queue"          # queue | now | schedule
    due_at: str | None = None    # ISO 8601 en UTC, solo para schedule
    ai_label: bool = True
    again: bool = False          # confirmar si ya se publicó en este canal

    def validate(self) -> None:
        self.text = self.text.strip()
        if not self.text:
            raise PublishError("Escribe el texto de la publicación.")
        if text_units(self.text) > MAX_TIKTOK_TEXT:
            raise PublishError(f"El texto pasa de {MAX_TIKTOK_TEXT} caracteres, el máximo de TikTok con video.")
        if self.mode not in MODES:
            raise PublishError("Modo de publicación no válido.")
        if self.mode == "schedule" and not self.due_at:
            raise PublishError("Elige la fecha y la hora de publicación.")


class Publisher:
    """Configuración, tareas de publicación en curso (una por video) e historial."""

    def __init__(self, db: sqlite3.Connection, http: Http = http_request,
                 drive_id: Callable[[Path], str | None] = drive_file_id,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep,
                 poll: float = 5, sync_timeout: float = 20 * 60, demo: bool = False,
                 default_folder: Path | None = None):
        self.db = db
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.http, self.drive_id, self.sleep = http, drive_id, sleep
        self.poll, self.sync_timeout, self.demo = poll, sync_timeout, demo
        self.default_folder = default_folder
        self.status: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ configuración

    def _get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", [key]).fetchone()
        return row[0] if row else default

    def _set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", [key, value])

    @property
    def key(self) -> str:
        # La variable de entorno manda; si no, la guardada desde la app (en data/, fuera de git).
        return os.environ.get("PAPER_STAGE_BUFFER_KEY") or self._get("buffer_key")

    def _roots(self) -> list[Path]:
        # En modo demo (y en las pruebas) nunca se toca el Drive real: solo la carpeta simulada.
        if self.demo:
            return [self.default_folder.parent] if self.default_folder else []
        return drive_roots()

    @property
    def folder(self) -> Path | None:
        value = self._get("drive_folder")
        if value and any(r == Path(value) or r in Path(value).parents for r in self._roots()):
            return Path(value)
        if self.demo:
            return self.default_folder
        roots = self._roots()
        return Path(value) if value else (roots[0] / DRIVE_FOLDER_NAME if roots else None)

    def config(self) -> dict:
        folder = self.folder
        key = self.key
        return {
            "buffer_key": bool(key), "buffer_key_hint": f"…{key[-4:]}" if len(key) > 8 else "",
            "buffer_key_env": bool(os.environ.get("PAPER_STAGE_BUFFER_KEY")),
            "channel_id": self._get("channel_id"), "channel_name": self._get("channel_name"),
            "drive_folder": str(folder) if folder else "", "drive_folder_exists": bool(folder and folder.is_dir()),
            "drive_roots": [str(r) for r in self._roots()], "ai_label": self._get("ai_label", "1") == "1",
            "ready": bool(key and self._get("channel_id") and folder and folder.is_dir()),
            "demo": self.demo,
        }

    def save_config(self, data: dict) -> dict:
        if data.get("buffer_key") is not None:
            key = str(data["buffer_key"]).strip()
            if key and (len(key) < 10 or any(c.isspace() for c in key)):
                raise PublishError("Esa clave de API no parece válida.")
            self._set("buffer_key", key)
        if data.get("channel_id") is not None:
            self._set("channel_id", str(data["channel_id"]))
            self._set("channel_name", str(data.get("channel_name") or ""))
        if data.get("drive_folder") is not None:
            folder = Path(str(data["drive_folder"])).expanduser()
            if str(data["drive_folder"]).strip() and not any(r in folder.parents for r in self._roots()):
                raise PublishError("La carpeta debe estar dentro de «Mi unidad» de Google Drive para escritorio."
                                   if not self.demo else "En modo demo solo se usa la carpeta simulada de data/.")
            if data.get("create_folder"):
                folder.mkdir(parents=True, exist_ok=True)
            self._set("drive_folder", str(folder) if str(data["drive_folder"]).strip() else "")
        if data.get("ai_label") is not None:
            self._set("ai_label", "1" if data["ai_label"] else "0")
        self.db.commit()
        return self.config()

    async def channels(self) -> list[dict]:
        if not self.key:
            raise PublishError("Primero guarda la clave de API de Buffer.")
        channels = await asyncio.to_thread(buffer_channels, self.key, self.http)
        return [c for c in channels if c["service"] == "tiktok"]

    # ------------------------------------------------------------------ historial

    def history(self, slug: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM publications WHERE slug = ? ORDER BY created_at DESC", [slug])
        cols = [c[0] for c in rows.description]
        return [dict(zip(cols, r)) for r in rows]

    def state(self, slug: str) -> dict:
        return {"status": self.status.get(slug), "history": self.history(slug)}

    def published(self) -> set[str]:
        """Slugs con alguna publicación registrada (por Buffer o marcada a mano)."""
        return {slug for (slug,) in self.db.execute("SELECT DISTINCT slug FROM publications")}

    def mark_manual(self, slug: str) -> dict:
        """Registra que el video ya se subió a TikTok a mano, para no volver a enviarlo."""
        if not (library.slug_dir(slug) / "final.mp4").exists():
            raise PublishError("Este video todavía no tiene final.mp4.")
        if any(h["mode"] == MANUAL for h in self.history(slug)):
            raise PublishError("Este video ya está marcado como publicado.")
        self.db.execute(
            "INSERT INTO publications (slug, channel_id, channel_name, post_id, mode, due_at, url, created_at) "
            "VALUES (?, ?, ?, NULL, ?, NULL, '', ?)",
            [slug, self._get("channel_id"), self._get("channel_name"), MANUAL, time.time()])
        self.db.commit()
        return self.state(slug)

    def unmark(self, slug: str, pub_id: int) -> dict:
        """Quita una marca manual (las enviadas por Buffer no se borran: están en Buffer)."""
        cur = self.db.execute("DELETE FROM publications WHERE id = ? AND slug = ? AND mode = ?",
                              [pub_id, slug, MANUAL])
        self.db.commit()
        if not cur.rowcount:
            raise PublishError("Solo se pueden quitar las marcas de «publicado a mano».")
        return self.state(slug)

    # ------------------------------------------------------------------ publicar

    def start(self, slug: str, req: PublishRequest | None) -> dict:
        """Arranca la publicación (o, con req=None, la prueba de la configuración)."""
        if slug in self.tasks and not self.tasks[slug].done():
            raise PublishError("Ya se está publicando este video.")
        cfg = self.config()
        if not cfg["buffer_key"] or not cfg["drive_folder_exists"]:
            raise PublishError("Falta configurar la publicación en Sistema (carpeta de Drive y clave de Buffer).")
        if req is not None:
            if not cfg["channel_id"]:
                raise PublishError("Elige en Sistema la cuenta de TikTok de Buffer.")
            req.validate()
            video = library.slug_dir(slug) / "final.mp4"
            if not video.exists():
                raise PublishError("Este video todavía no tiene final.mp4.")
            done = [h for h in self.history(slug) if h["channel_id"] == cfg["channel_id"] or h["mode"] == MANUAL]
            if done and not req.again:
                raise PublishError("Este video ya está publicado en TikTok (marcado a mano)."
                                   if any(h["mode"] == MANUAL for h in done)
                                   else "Este video ya se envió a esa cuenta de TikTok.")
        self.status[slug] = {"state": "running", "step": "Preparando…", "log": [], "started": time.time()}
        self.tasks[slug] = asyncio.create_task(self._run(slug, req, cfg))
        return self.state(slug)

    async def stop(self) -> None:
        tasks = [t for t in self.tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=5)

    def _step(self, slug: str, text: str) -> None:
        st = self.status[slug]
        st["step"] = text
        st["log"].append({"t": round(time.time() - st["started"], 1), "text": text})

    async def _run(self, slug: str, req: PublishRequest | None, cfg: dict) -> None:
        try:
            dest = await self._upload(slug, cfg)
            url = await self._wait_public(slug, dest)
            if req is None:
                self._step(slug, "Buffer: comprobando la clave y la cuenta de TikTok…")
                channels = await self.channels()
                if cfg["channel_id"] and not any(c["id"] == cfg["channel_id"] for c in channels):
                    raise PublishError("La cuenta de TikTok elegida ya no está en Buffer: elígela otra vez.")
                self.status[slug].update(state="done", step="Todo listo: Drive sirve el video y Buffer responde.")
                return
            post = await self._post(slug, req, cfg, url)
            self.db.execute(
                "INSERT INTO publications (slug, channel_id, channel_name, post_id, mode, due_at, url, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [slug, cfg["channel_id"], cfg["channel_name"], post.get("id"), req.mode,
                 post.get("dueAt") or req.due_at, url, time.time()])
            self.db.commit()
            when = {"now": "Buffer lo está publicando ahora.", "queue": "Quedó en tu cola de Buffer.",
                    "schedule": "Quedó programado en Buffer."}[req.mode]
            self.status[slug].update(state="done", step=f"Enviado. {when} Deja el video en Drive hasta que se publique.")
        except PublishError as exc:
            self.status[slug].update(state="failed", step=str(exc))
        except Exception as exc:  # noqa: BLE001 — cualquier fallo termina la publicación con un mensaje
            self.status[slug].update(state="failed", step=f"{type(exc).__name__}: {exc}")

    async def _upload(self, slug: str, cfg: dict) -> Path:
        folder = Path(cfg["drive_folder"])
        if slug == TEST_SLUG:
            dest = folder / "prueba-teatrito.mp4"
            if not dest.exists():
                self._step(slug, "Creando un video de prueba de 1 s…")
                await asyncio.to_thread(_test_video, dest)
            return dest
        src = library.slug_dir(slug) / "final.mp4"
        dest = folder / f"{slug}.mp4"
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            self._step(slug, "El video ya estaba en la carpeta de Drive.")
        else:
            self._step(slug, "Copiando final.mp4 a la carpeta de Google Drive…")
            await asyncio.to_thread(shutil.copyfile, src, dest)
        return dest

    async def _wait_public(self, slug: str, dest: Path) -> str:
        self._step(slug, "Esperando a que Drive lo suba (unos minutos para ~60 MB)…")
        deadline = time.monotonic() + self.sync_timeout
        detail = "Drive todavía no registró el archivo."
        while time.monotonic() < deadline:
            file_id = await asyncio.to_thread(self.drive_id, dest)
            if file_id:
                url = public_url(file_id)
                ok, detail = await asyncio.to_thread(check_video_url, url, self.http)
                if ok:
                    self._step(slug, "Drive ya sirve el video en un enlace público.")
                    return url
            await self.sleep(self.poll)
        raise PublishError(f"Pasaron {int(self.sync_timeout // 60)} min y el video no está disponible: {detail}")

    async def _post(self, slug: str, req: PublishRequest, cfg: dict, url: str) -> dict:
        self._step(slug, "Creando la publicación en Buffer…")
        publish = library.read_json(library.slug_dir(slug) / "publish.json")
        publish = publish if isinstance(publish, dict) else {}
        video: dict = {"url": url}
        with contextlib.suppress(TypeError, ValueError):
            video["metadata"] = {"thumbnailOffset": int(float(publish["frame_portada_s"]) * 1000)}
        post = {
            "text": req.text, "channelId": cfg["channel_id"], "schedulingType": "automatic",
            "mode": MODES[req.mode], "assets": [{"video": video}], "aiAssisted": True,
            "metadata": {"tiktok": {"isAiGenerated": bool(req.ai_label)}},
        }
        if req.mode == "schedule":
            post["dueAt"] = req.due_at
        return await asyncio.to_thread(buffer_create_post, self.key, post, self.http)


def _test_video(dest: Path) -> None:
    """Un MP4 vertical de 1 s para probar que Drive lo sirve en público."""
    if not shutil.which("ffmpeg"):
        raise PublishError("Hace falta ffmpeg para crear el video de prueba.")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=0xE9B949:s=540x960:r=24:d=1", "-pix_fmt", "yuv420p", str(dest)],
                   check=True, capture_output=True, timeout=60)
