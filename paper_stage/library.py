"""Lectura de output/<slug>/: progreso por pasos, QA, metadatos y portada."""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from .core import LIA, OUTPUT_DIR, SLUG_RE, slugify

# Paso del pipeline (§1 del prompt) → archivos que indican que está hecho.
# "a|b" vale con cualquiera de los dos: los videos anteriores al QA previo no tienen
# qa_previo.md, pero si ya se renderizaron, ese paso quedó atrás.
STEPS: list[tuple[str, str, list[str]]] = [
    ("research", "Investigación", ["research.md"]),
    ("bible", "Biblia visual", ["bible.md"]),
    ("script", "Guion", ["script.json"]),
    ("voice", "Voz", ["audio/scene_*.wav"]),
    ("timing", "Tiempos", ["timeline.json"]),
    ("sync", "Sincronía", ["words.json", "mouth.json"]),
    ("stage", "Animación", ["stage.html"]),
    ("audio", "Música y mezcla", ["mix.wav"]),
    ("preqa", "QA previo", ["qa_previo.md|final.mp4"]),
    ("render", "Render", ["final.mp4"]),
    ("publish", "Publicación", ["publish.json"]),
    ("qa", "QA final", ["qa.md"]),
]

TEXT_FILES = ["research.md", "bible.md", "qa_previo.md", "qa.md"]
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
        done = bool(out_dir.is_dir()) and all(
            any(any(out_dir.glob(alt)) for alt in p.split("|")) for p in patterns)
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


def _publish(out_dir: Path) -> dict:
    publish = read_json(out_dir / "publish.json")
    return publish if isinstance(publish, dict) else {}


def _cover_time(publish: dict) -> float:
    try:
        return float(publish.get("frame_portada_s", 1.5))
    except (TypeError, ValueError):
        return 1.5


def _fresh(target: Path, *sources: Path) -> bool:
    return target.exists() and all(
        target.stat().st_mtime >= src.stat().st_mtime for src in sources if src.exists())


# Oculta los subtítulos y escribe texto_portada en su franja, con su misma letra y el
# amarillo mostaza de la palabra activa, reduciéndolo hasta que quepa.
_COVER_JS = """(text) => {
  const subs = document.getElementById("subs");
  let box = {x: innerWidth * .08, y: innerHeight * .77, w: innerWidth * .84, h: innerHeight * .1};
  if (subs) {
    const r = subs.getBoundingClientRect();
    if (r.width && r.height) box = {x: r.left, y: r.top, w: r.width, h: r.height};
    subs.style.visibility = "hidden";
  }
  if (!text) return;
  const el = document.createElement("div");
  Object.assign(el.style, {
    position: "fixed", left: box.x + "px", top: box.y + "px", width: box.w + "px", height: box.h + "px",
    display: "flex", alignItems: "center", justifyContent: "center", textAlign: "center",
    padding: "0 " + box.w * .04 + "px", boxSizing: "border-box",
    fontFamily: "'Fredoka', sans-serif", fontWeight: "700", lineHeight: "1.05",
    color: "#E9B949", zIndex: 99999,
  });
  el.textContent = text;
  document.body.appendChild(el);
  let size = box.h * .6;
  do { el.style.fontSize = size + "px"; size -= 2; }
  while ((el.scrollHeight > box.h || el.scrollWidth > box.w) && size > 20);
}"""


def cover(out_dir: Path) -> Path | None:
    """Genera portada.jpg a tamaño completo para subirla a las redes: el fotograma de
    frame_portada_s de stage.html, sin subtítulos y con texto_portada en su franja."""
    stage, video, image = out_dir / "stage.html", out_dir / "final.mp4", out_dir / "portada.jpg"
    if not stage.exists() or not video.exists():
        return None
    if _fresh(image, stage, video, out_dir / "publish.json"):
        return image
    size = _video_size(video)
    if not size:
        return None
    publish = _publish(out_dir)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": size[0], "height": size[1]})
                page.goto(stage.resolve().as_uri())
                page.wait_for_function(
                    "typeof window.renderAt === 'function' && window.READY !== false", timeout=30000)
                page.evaluate("document.fonts.ready")
                # Sin devolver nada: si renderAt devuelve un objeto grande (p. ej. una timeline
                # de GSAP), evaluate intenta serializarlo y se cuelga.
                page.evaluate(f"() => {{ window.renderAt({_cover_time(publish)}); }}")
                page.evaluate(_COVER_JS, str(publish.get("texto_portada") or "").strip())
                page.screenshot(path=str(image), type="jpeg", quality=92)
            finally:
                browser.close()
    except Exception:  # noqa: BLE001 — sin portada no se rompe nada: queda el fotograma del video
        image.unlink(missing_ok=True)
        return None
    return image if image.exists() else None


def _video_size(video: Path) -> tuple[int, int] | None:
    if not shutil.which("ffprobe"):
        return None
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", str(video)], capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    with contextlib.suppress(ValueError):
        w, h = (int(n) for n in out.split("x")[:2])
        return w, h
    return None


def poster(out_dir: Path) -> Path | None:
    """Genera (una vez) poster.jpg, la miniatura de la app: portada.jpg si existe o, si no,
    el fotograma de final.mp4 en el segundo indicado en publish.json."""
    video, image, full = out_dir / "final.mp4", out_dir / "poster.jpg", out_dir / "portada.jpg"
    if not video.exists():
        return None
    if _fresh(image, video, full):
        return image
    if not shutil.which("ffmpeg"):
        return None
    source = ["-i", str(full)] if full.exists() else \
        ["-ss", str(_cover_time(_publish(out_dir))), "-i", str(video)]
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *source,
         "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4", str(image)],
        capture_output=True, timeout=60,
    )
    return image if image.exists() else None


# Lo que la app comprueba por su cuenta en cada video terminado, sin fiarse de qa.md.
SIZES = {"vertical": (1080, 1920), "horizontal": (1920, 1080)}
MAX_HASHTAGS, MAX_SHORT_DESCRIPTION, MAX_TITLE = 5, 150, 60
# Gancho (§2 y §7 del prompt): la voz arranca casi en el frame 1 y el gancho escrito es corto.
MAX_VOICE_START, MAX_HOOK_WORDS = 0.3, 6
VOICE_SILENCE_DB = -35  # el mismo umbral de "hay voz" que el QA del prompt


def _probe(video: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)],
        capture_output=True, text=True, timeout=60,
    ).stdout
    return json.loads(out or "{}")


def _loudness(video: Path) -> float | None:
    err = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-vn",
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300,
    ).stderr
    match = re.search(r'"input_i"\s*:\s*"(-?[\d.]+|-inf)"', err)
    return float(match.group(1)) if match and match.group(1) != "-inf" else None


def _voice_start(voice: Path) -> float | None:
    """Segundo en que empieza la voz (primer tramo por encima de −35 dBFS); None si no se oye."""
    err = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(voice),
         "-af", f"silencedetect=noise={VOICE_SILENCE_DB}dB:d=0.02", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    ).stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", err)]
    if not starts or starts[0] > 0.01:
        return 0.0  # no empieza en silencio
    return ends[0] if ends else None  # sin silence_end: todo el archivo es silencio


def _checks(out_dir: Path) -> list[dict]:
    video = out_dir / "final.mp4"
    info = _probe(video)
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    script, publish = read_json(out_dir / "script.json"), _publish(out_dir)
    formato = script.get("formato") if isinstance(script, dict) else None
    checks = []

    def check(label: str, ok: bool, detail: str) -> None:
        checks.append({"label": label, "ok": bool(ok), "detail": detail})

    total = float((info.get("format") or {}).get("duration") or 0)
    check("Duración entre 60 y 65 s", 60.0 <= total <= 65.0, f"{total:.2f} s")
    size = (v.get("width"), v.get("height"))
    expected = SIZES.get(formato or "", None)
    check("Resolución", size == expected if expected else size in SIZES.values(),
          f"{size[0]}×{size[1]}" + (f" (se esperaba {expected[0]}×{expected[1]})" if expected and size != expected else ""))
    check("24 fps", v.get("r_frame_rate") == "24/1", str(v.get("r_frame_rate")))
    check("Video H.264 yuv420p", v.get("codec_name") == "h264" and v.get("pix_fmt") == "yuv420p",
          f"{v.get('codec_name')} {v.get('pix_fmt')}")
    check("Audio AAC 48 kHz", a.get("codec_name") == "aac" and a.get("sample_rate") == "48000",
          f"{a.get('codec_name')} {a.get('sample_rate')} Hz" if a else "sin audio")
    if a and v:
        gap = abs(float(a.get("duration") or 0) - float(v.get("duration") or 0))
        check("Audio y video duran lo mismo", gap <= 0.1, f"diferencia de {gap:.2f} s")
    lufs = _loudness(video) if a else None
    check("Volumen −14 LUFS ±1", lufs is not None and abs(lufs + 14) <= 1,
          f"{lufs:.1f} LUFS" if lufs is not None else "no se pudo medir")
    voice = out_dir / "voice.wav"
    start = _voice_start(voice) if voice.exists() else None
    check(f"La voz empieza en ≤{MAX_VOICE_START} s", start is not None and start <= MAX_VOICE_START,
          f"{start:.2f} s" if start is not None else ("sin voz en voice.wav" if voice.exists() else "falta voice.wav"))
    script = script if isinstance(script, dict) else {}
    # Los videos anteriores al gancho escrito no tienen estos campos: no se les exige.
    if "texto_gancho" in script or "tipo_gancho" in script:
        hook = " ".join(str(script.get("texto_gancho") or "").split())
        words = len(hook.split())
        check(f"Gancho escrito de {MAX_HOOK_WORDS} palabras como máximo", 0 < words <= MAX_HOOK_WORDS,
              f"«{hook}» ({words} palabras)" if hook else "falta texto_gancho")
    hashtags = publish.get("hashtags") or []
    check(f"{MAX_HASHTAGS} hashtags", len(hashtags) == MAX_HASHTAGS, f"{len(hashtags)}")
    short = str(publish.get("descripcion_corta") or "")
    check(f"Descripción corta de {MAX_SHORT_DESCRIPTION} caracteres como máximo",
          0 < len(short) <= MAX_SHORT_DESCRIPTION, f"{len(short)} caracteres" if short else "falta")
    long_titles = [t for t in publish.get("titulos") or [] if len(str(t)) > MAX_TITLE]
    check(f"Títulos de {MAX_TITLE} caracteres como máximo", bool(publish.get("titulos")) and not long_titles,
          f"{len(long_titles)} demasiado largos" if long_titles else f"{len(publish.get('titulos') or [])} títulos")
    return checks


def verify(out_dir: Path, compute: bool = True) -> dict | None:
    """Revisión automática de final.mp4 y publish.json, guardada en verify.json mientras
    no cambien. Con compute=False solo devuelve la guardada (para listados rápidos)."""
    video, cache = out_dir / "final.mp4", out_dir / "verify.json"
    if not video.exists():
        return None
    sources = [p for p in (video, out_dir / "publish.json", out_dir / "script.json", out_dir / "voice.wav")
               if p.exists()]
    stamp = [round(p.stat().st_mtime, 3) for p in sources]
    cached = read_json(cache)
    if isinstance(cached, dict) and cached.get("stamp") == stamp:
        return cached
    if not compute or not (shutil.which("ffprobe") and shutil.which("ffmpeg")):
        return None
    try:
        checks = _checks(out_dir)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    result = {"stamp": stamp, "ok": all(c["ok"] for c in checks), "checks": checks}
    with contextlib.suppress(OSError):
        cache.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _title(out_dir: Path) -> str:
    script = read_json(out_dir / "script.json")
    if isinstance(script, dict) and script.get("titulo_crayon"):
        return str(script["titulo_crayon"])
    return out_dir.name.replace("-", " ").capitalize()


def script_character(script: dict | None) -> str:
    """ID del personaje de un script.json. Los videos anteriores a los personajes no lo
    indican (son de Lía), y alguno trae el nombre («Lía») en vez del ID: slugify los iguala."""
    value = script.get("personaje") if isinstance(script, dict) else None
    return slugify(str(value)) if value else LIA.id


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
        "personaje": script_character(script),
        "gancho": script.get("gancho"),
        "tipo_gancho": script.get("tipo_gancho"),
        "has_video": final.exists(),
        "has_stage": (out_dir / "stage.html").exists(),
        "duration": duration(final),
        "steps_done": sum(s["done"] for s in steps),
        "steps_total": len(steps),
        "qa": qa_summary(out_dir),
        "verify": verify(out_dir, compute=False),
        "updated": max((p.stat().st_mtime for p in out_dir.iterdir()), default=out_dir.stat().st_mtime),
    }


def list_videos() -> list[dict]:
    if not OUTPUT_DIR.is_dir():
        return []
    dirs = [d for d in OUTPUT_DIR.iterdir() if d.is_dir() and SLUG_RE.match(d.name)]
    return sorted((summary(d) for d in dirs), key=lambda v: v["updated"], reverse=True)


def videos_of(personaje: str, limit: int = 3) -> list[str]:
    """Slugs de los videos terminados de un personaje, del más reciente al más antiguo."""
    return [v["slug"] for v in list_videos() if v["personaje"] == personaje and v["has_video"]][:limit]


def hooks_of(personaje: str, limit: int = 6) -> list[str]:
    """Ganchos de los últimos videos de un personaje («tipo: gancho»), del más reciente al más antiguo."""
    hooks = []
    for v in list_videos():
        if v["personaje"] == personaje and v.get("gancho"):
            hooks.append(f"{v['tipo_gancho']}: {v['gancho']}" if v.get("tipo_gancho") else str(v["gancho"]))
    return hooks[:limit]


def detail(slug: str) -> dict | None:
    out_dir = slug_dir(slug)
    if not out_dir.is_dir():
        return None
    data = summary(out_dir)
    data["steps"] = steps_for(out_dir)
    data["verify"] = verify(out_dir)
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
