"""Minidramas (en preparación): la clave de la API de xAI (Grok Imagine) y la comprobación de su acceso.

La clave se guarda en data/ (tabla settings) o llega por PAPER_STAGE_XAI_KEY; solo viaja en la cabecera a api.x.ai,
nunca vuelve al navegador ni al agente de producción. La comprobación solo lista modelos: no genera nada ni gasta
cupo. Que la clave valga no prueba que el plan de X cubra video: eso lo dice el primer clip de prueba.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Callable

XAI_API = "https://api.x.ai/v1"
VIDEO_PREFIX = "grok-imagine-video"
# xAI reparte los modelos en varias listas; la de video puede no existir (404) y no es un error.
MODEL_LISTS = ["/models", "/video-generation-models"]


class DramaError(Exception):
    pass


def _request(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DramaError(f"No hay conexión con api.x.ai: {exc}") from exc


def model_ids(payload: object) -> list[str]:
    """IDs de modelos en una respuesta de xAI ({"data": [...]} o {"models": [...]})."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("data") or payload.get("models") or []
    ids = []
    for item in items if isinstance(items, list) else []:
        mid = item.get("id") if isinstance(item, dict) else None
        if isinstance(mid, str) and mid and mid not in ids:
            ids.append(mid)
    return ids


class Dramas:
    def __init__(self, db: sqlite3.Connection, demo: bool = False, http: Callable = _request):
        self.db, self.demo, self.http = db, demo, http

    def _get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", [key]).fetchone()
        return row[0] if row else default

    def _set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", [key, value])
        self.db.commit()

    @property
    def key(self) -> str:
        if self.demo:  # el modo demo nunca usa la red ni la clave
            return ""
        return os.environ.get("PAPER_STAGE_XAI_KEY") or self._get("xai_key")

    def config(self) -> dict:
        key = self.key
        try:
            check = json.loads(self._get("xai_check") or "null")
        except ValueError:
            check = None
        return {"xai_key": bool(key), "xai_key_hint": f"…{key[-4:]}" if len(key) > 8 else "",
                "xai_key_env": bool(os.environ.get("PAPER_STAGE_XAI_KEY")), "demo": self.demo, "check": check}

    def save_config(self, data: dict) -> dict:
        if data.get("xai_key") is not None:
            key = str(data["xai_key"]).strip()
            if key and (len(key) < 10 or any(c.isspace() for c in key)):
                raise DramaError("Esa clave de API no parece válida.")
            self._set("xai_key", key)
            self._set("xai_check", "")  # la comprobación anterior era de otra clave
        return self.config()

    def check(self) -> dict:
        """Lista los modelos a los que da acceso la clave y si entre ellos hay de video. No gasta cupo."""
        if self.demo:
            raise DramaError("En modo demo no se usa la API de xAI.")
        key = self.key
        if not key:
            raise DramaError("Guarda antes la clave de API de xAI.")
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        models: list[str] = []
        for path in MODEL_LISTS:
            status, raw = self.http("GET", XAI_API + path, headers, None, 30)
            if status in (401, 403):
                raise DramaError("xAI rechazó la clave: revisa que esté bien copiada y activa.")
            if status == 404:
                continue
            if status >= 400:
                raise DramaError(f"xAI respondió {status} al listar los modelos.")
            try:
                models += [m for m in model_ids(json.loads(raw)) if m not in models]
            except ValueError:
                raise DramaError("xAI devolvió una respuesta que no es JSON.")
        video = [m for m in models if m.startswith(VIDEO_PREFIX)]
        result = {"ok": bool(video), "at": time.time(), "models": models, "video_models": video,
                  "message": (f"La clave da acceso a {len(video)} modelo(s) de video: {', '.join(video)}. Falta probar"
                              " un clip para saber cuánto cupo gasta." if video else
                              "La clave vale, pero no aparece ningún modelo de video (grok-imagine-video).")}
        self._set("xai_check", json.dumps(result, ensure_ascii=False))
        return result
