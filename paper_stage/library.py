"""Lectura de output/<slug>/: progreso por pasos, QA, metadatos y portada."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .core import OUTPUT_DIR, SLUG_RE

# Paso del pipeline (§1 del prompt) → archivos que indican que está hecho.
STEPS: list[tuple[str, str, list[str]]] = [
    ("research", "Investigación", ["research.md"]),
    ("bible", "Biblia visual", ["bible.md"]),
    ("script", "Guion", ["script.json"]),
    ("voice", "Voz", ["audio/scene_*.wav"]),
    ("timing", "Tiempos", ["timeline.json"]),
    ("sync", "Sincronía", ["words.json", "mouth.json"]),
    ("stage", "Animación", ["stage.html"]),
    ("audio", "Música y mezcla", ["mix.wav"]),
    ("render", "Render", ["final.mp4"]),
    ("qa", "QA", ["qa.md"]),
    ("publish", "Publicación", ["publish.json"]),
]

TEXT_FILES = ["research.md", "bible.md", "qa.md"]
JSON_FILES = ["script.json", "publish.json", "timeline.json"]
PUBLIC_SUFFIXES = {".mp4", ".html", ".md", ".json", ".srt", ".wav", ".jpg", ".png", ".txt"}

_duration_cache: dict[tuple[str, float], float | None] = {}


def slug_dir(slug: str) -> Path:
    if not SLUG_RE.match(slug):
        raise ValueError("slug no válido")
    return OUTPUT_DIR / slug


def steps_for(out_dir: Path) -> list[dict]:
    result = []
    for key, label, patterns in STEPS:
        done = bool(out_dir.is_dir()) and all(any(out_dir.glob(p)) for p in patterns)
        result.append({"key": key, "label": label, "done": done})
    return result


def qa_summary(out_dir: Path) -> dict | None:
    path = out_dir / "qa.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    passed = len(re.findall(r"^\s*[-*]\s*\[[xX✓]\]", text, re.M))
    failed = len(re.findall(r"^\s*[-*]\s*\[ \]", text, re.M))
    return {"passed": passed, "failed": failed, "total": passed + failed}


def read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def duration(path: Path) -> float | None:
    if not path.exists() or not shutil.which("ffprobe"):
        return None
    key = (str(path), path.stat().st_mtime)
    if key not in _duration_cache:
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=20,
            ).stdout.strip()
            _duration_cache[key] = round(float(out), 2)
        except (ValueError, subprocess.SubprocessError):
            _duration_cache[key] = None
    return _duration_cache[key]


def poster(out_dir: Path) -> Path | None:
    """Genera (una vez) poster.jpg desde final.mp4 en el segundo indicado en publish.json."""
    video, image = out_dir / "final.mp4", out_dir / "poster.jpg"
    if not video.exists():
        return None
    if image.exists() and image.stat().st_mtime >= video.stat().st_mtime:
        return image
    if not shutil.which("ffmpeg"):
        return None
    publish = read_json(out_dir / "publish.json") or {}
    try:
        at = float(publish.get("frame_portada_s", 1.5)) if isinstance(publish, dict) else 1.5
    except (TypeError, ValueError):
        at = 1.5
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(at), "-i", str(video),
         "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4", str(image)],
        capture_output=True, timeout=60,
    )
    return image if image.exists() else None


def _title(out_dir: Path) -> str:
    script = read_json(out_dir / "script.json")
    if isinstance(script, dict) and script.get("titulo_crayon"):
        return str(script["titulo_crayon"])
    return out_dir.name.replace("-", " ").capitalize()


def summary(out_dir: Path) -> dict:
    script = read_json(out_dir / "script.json")
    script = script if isinstance(script, dict) else {}
    steps = steps_for(out_dir)
    final = out_dir / "final.mp4"
    return {
        "slug": out_dir.name,
        "title": _title(out_dir),
        "idioma": script.get("idioma"),
        "formato": script.get("formato"),
        "gancho": script.get("gancho"),
        "has_video": final.exists(),
        "has_stage": (out_dir / "stage.html").exists(),
        "duration": duration(final),
        "steps_done": sum(s["done"] for s in steps),
        "steps_total": len(steps),
        "qa": qa_summary(out_dir),
        "updated": max((p.stat().st_mtime for p in out_dir.iterdir()), default=out_dir.stat().st_mtime),
    }


def list_videos() -> list[dict]:
    if not OUTPUT_DIR.is_dir():
        return []
    dirs = [d for d in OUTPUT_DIR.iterdir() if d.is_dir() and SLUG_RE.match(d.name)]
    return sorted((summary(d) for d in dirs), key=lambda v: v["updated"], reverse=True)


def detail(slug: str) -> dict | None:
    out_dir = slug_dir(slug)
    if not out_dir.is_dir():
        return None
    data = summary(out_dir)
    data["steps"] = steps_for(out_dir)
    data["texts"] = {
        name: (out_dir / name).read_text(encoding="utf-8", errors="replace")
        for name in TEXT_FILES if (out_dir / name).exists()
    }
    data["json"] = {name: read_json(out_dir / name) for name in JSON_FILES if (out_dir / name).exists()}
    data["files"] = sorted(
        (
            {"name": str(p.relative_to(out_dir)), "size": p.stat().st_size}
            for p in out_dir.rglob("*")
            if p.is_file() and p.suffix in PUBLIC_SUFFIXES and "frames" not in p.parts
        ),
        key=lambda f: f["name"],
    )
    data["has_mix"] = (out_dir / "mix.wav").exists()
    return data


def delete(slug: str) -> bool:
    out_dir = slug_dir(slug)
    if not out_dir.is_dir():
        return False
    shutil.rmtree(out_dir)
    return True
