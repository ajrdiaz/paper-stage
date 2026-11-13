"""Voz con ElevenLabs (en prueba): la clave, la comprobación de acceso y muestras para elegir la voz de oído.

La clave se guarda en data/ (tabla settings) o llega por PAPER_STAGE_ELEVENLABS_KEY; solo viaja en la cabecera
xi-api-key a api.elevenlabs.io y nunca vuelve al navegador. Comprobar solo lee el plan y las voces: no gasta
créditos. Las muestras sí los gastan (un crédito por carácter en v3 y Multilingual v2), así que solo se generan
cuando el usuario lo pide, y se guardan en data/voces/muestras/ para escucharlas sin volver a pagarlas.
Claude no puede oír: la voz de una serie no cambia hasta que el usuario elige una muestra.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import Callable

from . import library

API = "https://api.elevenlabs.io"
RATE = 24000  # pcm_24000 entra en todos los planes; 44,1 kHz exige el plan Pro
# Modelo → créditos por carácter. v3 es el más expresivo; Multilingual v2, el más estable; Flash, el barato.
MODELS = {"eleven_v3": 1.0, "eleven_multilingual_v2": 1.0, "eleven_flash_v2_5": 0.5}
MODEL_NAMES = {"eleven_v3": "Eleven v3", "eleven_multilingual_v2": "Multilingual v2", "eleven_flash_v2_5": "Flash v2.5"}
MAX_SAMPLE_VOICES = 6
SEED = 7
_ID_RE = re.compile(r"^[A-Za-z0-9]{8,40}$")


class ElevenError(Exception):
    pass


def _request(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ElevenError(f"No hay conexión con api.elevenlabs.io: {exc}") from exc


def _detail(raw: bytes) -> str:
    """El motivo de un error de ElevenLabs ({"detail": {"status", "message"}} o {"detail": "…"})."""
    try:
        detail = json.loads(raw).get("detail")
    except (ValueError, AttributeError):
        return ""
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("status") or "")
    return str(detail or "")


def _status(raw: bytes) -> str:
    try:
        detail = json.loads(raw).get("detail")
        return str(detail.get("status", "")) if isinstance(detail, dict) else ""
    except (ValueError, AttributeError):
        return ""


def words_from_alignment(alignment: dict | None, scene: int | None = None) -> list[dict]:
    """Palabras con su inicio y fin a partir de los tiempos por carácter, en el formato de words.json."""
    if not isinstance(alignment, dict):
        return []
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    words, current = [], None
    for ch, start, end in zip(chars, starts, ends):
        if ch.isspace():
            current = None
            continue
        if current is None:
            current = {"w": "", "start": round(float(start), 3), "end": round(float(end), 3)}
            if scene is not None:
                current["scene"] = scene
            words.append(current)
        current["w"] += ch
        current["end"] = round(float(end), 3)
    return words


def write_wav(path: Path, pcm: bytes, rate: int = RATE) -> float:
    """PCM de 16 bits mono a WAV. Devuelve la duración en segundos."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return len(pcm) / 2 / rate


def _voice(item: dict) -> dict:
    labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
    langs = [v.get("language") for v in item.get("verified_languages") or [] if isinstance(v, dict)]
    return {"id": item.get("voice_id", ""), "name": item.get("name") or item.get("voice_id", ""),
            "category": item.get("category") or "", "preview_url": item.get("preview_url") or "",
            "gender": labels.get("gender", ""), "accent": labels.get("accent", ""), "age": labels.get("age", ""),
            "descripcion": labels.get("description") or labels.get("descriptive") or "",
            "es": "es" in langs or labels.get("language") == "es"}


class ElevenLabs:
    def __init__(self, db: sqlite3.Connection, data_dir: Path, demo: bool = False, http: Callable = _request):
        self.db, self.demo, self.http = db, demo, http
        self.samples_dir = Path(data_dir) / "voces" / "muestras"
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

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
        return os.environ.get("PAPER_STAGE_ELEVENLABS_KEY") or self._get("elevenlabs_key")

    def _check(self) -> dict | None:
        try:
            return json.loads(self._get("elevenlabs_check") or "null")
        except ValueError:
            return None

    def config(self) -> dict:
        key = self.key
        return {"key": bool(key), "key_hint": f"…{key[-4:]}" if len(key) > 8 else "",
                "key_env": bool(os.environ.get("PAPER_STAGE_ELEVENLABS_KEY")), "demo": self.demo,
                "check": self._check(), "models": [{"id": m, "name": MODEL_NAMES[m], "credits": c}
                                                    for m, c in MODELS.items()],
                "max_voices": MAX_SAMPLE_VOICES, "samples": self.samples()}

    def save_config(self, data: dict) -> dict:
        if data.get("elevenlabs_key") is not None:
            key = str(data["elevenlabs_key"]).strip()
            if key and (len(key) < 10 or any(c.isspace() for c in key)):
                raise ElevenError("Esa clave de API no parece válida.")
            self._set("elevenlabs_key", key)
            self._set("elevenlabs_check", "")  # la comprobación anterior era de otra clave
        return self.config()

    def _headers(self, accept: str = "application/json") -> dict:
        key = self.key
        if self.demo:
            raise ElevenError("En modo demo no se usa la API de ElevenLabs.")
        if not key:
            raise ElevenError("Guarda antes la clave de API de ElevenLabs.")
        return {"xi-api-key": key, "Accept": accept, "Content-Type": "application/json"}

    def check(self) -> dict:
        """Lee el plan, los créditos y las voces de la cuenta. No gasta créditos."""
        headers = self._headers()
        voices: list[dict] = []
        token = ""
        for _ in range(5):  # hasta 500 voces; las de la biblioteca hay que añadirlas antes a «Mis voces»
            query = {"page_size": "100", **({"next_page_token": token} if token else {})}
            status, raw = self.http("GET", f"{API}/v2/voices?{urllib.parse.urlencode(query)}", headers, None, 30)
            if status in (401, 403):
                if _status(raw) == "missing_permissions":
                    raise ElevenError("La clave no tiene permiso para leer las voces: créala con acceso a "
                                      "Voices (lectura) y Text to Speech.")
                raise ElevenError("ElevenLabs rechazó la clave: revisa que esté bien copiada y activa.")
            if status >= 400:
                raise ElevenError(f"ElevenLabs respondió {status} al listar las voces. {_detail(raw)}".strip())
            try:
                payload = json.loads(raw)
            except ValueError:
                raise ElevenError("ElevenLabs devolvió una respuesta que no es JSON.")
            voices += [_voice(v) for v in payload.get("voices") or [] if isinstance(v, dict) and v.get("voice_id")]
            token = payload.get("next_page_token") or ""
            if not payload.get("has_more") or not token:
                break
        # El plan es un dato útil, no imprescindible: una clave sin permiso de usuario igual sirve para la voz.
        plan = {}
        status, raw = self.http("GET", f"{API}/v1/user/subscription", headers, None, 30)
        if status < 400:
            try:
                sub = json.loads(raw)
                plan = {"tier": sub.get("tier", ""), "used": sub.get("character_count"),
                        "limit": sub.get("character_limit"), "reset": sub.get("next_character_count_reset_unix")}
            except (ValueError, AttributeError):
                plan = {}
        voices.sort(key=lambda v: (not v["es"], v["category"] == "premade", v["name"].lower()))
        left = (plan["limit"] - plan["used"]) if isinstance(plan.get("limit"), int) and isinstance(plan.get("used"), int) else None
        message = f"{len(voices)} voces en tu cuenta."
        if left is not None:
            message += f" Plan {plan['tier'] or '—'}: quedan {left:,} de {plan['limit']:,} créditos.".replace(",", ".")
        result = {"ok": bool(voices), "at": time.time(), "plan": plan, "left": left, "voices": voices,
                  "message": message}
        self._set("elevenlabs_check", json.dumps(result, ensure_ascii=False))
        return result

    def synthesize(self, text: str, voice_id: str, model: str, out: Path, scene: int | None = None) -> list[dict]:
        """Genera la voz de un texto en un WAV de 24 kHz y devuelve sus palabras con tiempos (si los da el modelo)."""
        if model not in MODELS:
            raise ElevenError("Modelo de ElevenLabs no válido.")
        if not _ID_RE.match(voice_id or ""):
            raise ElevenError("Voz no válida.")
        text = text.strip()
        if not text:
            raise ElevenError("No hay texto para la voz.")
        headers = self._headers()
        body = json.dumps({"text": text, "model_id": model, "seed": SEED}).encode()
        base = f"{API}/v1/text-to-speech/{voice_id}"
        status, raw = self.http("POST", f"{base}/with-timestamps?output_format=pcm_{RATE}", headers, body, 180)
        if status in (400, 422):
            # Si el modelo no da tiempos, se pide solo el audio (un pedido rechazado no cobra créditos).
            status, raw = self.http("POST", f"{base}?output_format=pcm_{RATE}",
                                    {**headers, "Accept": "audio/pcm"}, body, 180)
            if status < 400:
                write_wav(out, raw)
                return []
        if status in (401, 403):
            raise ElevenError("ElevenLabs rechazó la clave o le falta el permiso de Text to Speech.")
        if status == 429:
            raise ElevenError("ElevenLabs está ocupado o se acabaron los créditos del mes (429). Prueba más tarde.")
        if status >= 400:
            raise ElevenError(f"ElevenLabs respondió {status}: {_detail(raw) or 'sin detalle'}.")
        try:
            payload = json.loads(raw)
            pcm = base64.b64decode(payload["audio_base64"])
        except (ValueError, KeyError, TypeError):
            raise ElevenError("ElevenLabs devolvió una respuesta inesperada.")
        write_wav(out, pcm)
        return words_from_alignment(payload.get("alignment"), scene)

    # ------------------------------------------------------------------ muestras

    def sample_text(self, slug: str, scene: int) -> str:
        try:
            script = library.read_json(library.slug_dir(slug) / "script.json")
        except ValueError:
            raise ElevenError("Video no válido.")
        scenes = script.get("escenas") if isinstance(script, dict) else None
        for item in scenes or []:
            if isinstance(item, dict) and item.get("n") == scene and str(item.get("vo") or "").strip():
                return str(item["vo"]).strip()
        raise ElevenError("Ese video no tiene texto para esa escena.")

    def estimate(self, slug: str, scene: int, voices: list[str], model: str) -> int:
        return round(len(self.sample_text(slug, scene)) * MODELS.get(model, 1.0) * len(voices))

    def make_samples(self, slug: str, scene: int, voices: list[str], model: str) -> dict:
        """La misma escena con varias voces, para elegir de oído. Gasta créditos: solo a petición del usuario."""
        voices = [v for v in dict.fromkeys(voices) if v]
        if not voices:
            raise ElevenError("Elige al menos una voz.")
        if len(voices) > MAX_SAMPLE_VOICES:
            raise ElevenError(f"Como mucho {MAX_SAMPLE_VOICES} voces por tanda.")
        text = self.sample_text(slug, scene)
        made = []
        for voice in voices:
            out = self.samples_dir / slug / f"{scene:02d}" / model / f"{voice}.wav"
            words = self.synthesize(text, voice, model, out, scene)
            out.with_suffix(".json").write_text(json.dumps({"texto": text, "palabras": words, "at": time.time()},
                                                           ensure_ascii=False), encoding="utf-8")
            made.append(voice)
        return {"made": made, "credits": round(len(text) * MODELS[model] * len(made))}

    def sample_path(self, slug: str, scene: str, model: str, voice: str) -> Path | None:
        if not (re.fullmatch(r"\d{2}", scene) and model in MODELS and _ID_RE.match(voice)):
            return None
        try:
            library.slug_dir(slug)
        except ValueError:
            return None
        path = self.samples_dir / slug / scene / model / f"{voice}.wav"
        return path if path.is_file() else None

    def samples(self) -> list[dict]:
        """Las muestras ya generadas, agrupadas por video y escena, con la voz actual (Chatterbox) para comparar."""
        if not self.samples_dir.is_dir():
            return []
        names = {v["id"]: v["name"] for v in (self._check() or {}).get("voices") or []}
        groups = []
        for scene_dir in sorted(self.samples_dir.glob("*/*")):
            slug, scene = scene_dir.parent.name, scene_dir.name
            if not (re.fullmatch(r"\d{2}", scene) and library.SLUG_RE.match(slug)):
                continue
            items = []
            for wav in sorted(scene_dir.glob("*/*.wav"), key=lambda p: p.stat().st_mtime):
                model, voice = wav.parent.name, wav.stem
                if model not in MODELS or not _ID_RE.match(voice):
                    continue
                items.append({"voice": voice, "name": names.get(voice, voice), "model": model,
                              "model_name": MODEL_NAMES[model],
                              "url": f"/voces/muestras/{slug}/{scene}/{model}/{voice}.wav"})
            if not items:
                continue
            current = library.slug_dir(slug) / "audio" / f"raw_{scene}.wav"
            text = (library.read_json(scene_dir / items[0]["model"] / f"{items[0]['voice']}.json") or {}).get("texto", "")
            groups.append({"slug": slug, "scene": int(scene), "texto": text, "items": items,
                           "actual": f"/files/{slug}/audio/raw_{scene}.wav" if current.is_file() else ""})
        return groups
