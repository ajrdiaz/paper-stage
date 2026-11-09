"""Revisión de audio y video de cada video terminado, con sugerencias de corrección.

Claude no puede oír: el audio se valida transcribiéndolo y comparándolo con el guion, y midiendo
el sonido. El video sí se ve, en fotogramas cada pocos segundos, además de medir congelados, negros y
cortes. Lo que no se mide (naturalidad y entonación de la voz) lo sigue juzgando quien lo escucha.
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import re
import subprocess
import time
import unicodedata
from pathlib import Path

from . import library
from .radar import RadarError, _extract_json, ask_claude

FILE = "revision.json"
FRAMES_DIR = "revision"
FRAME_EVERY = 3  # segundos entre fotogramas: bastante para ver cada escena sin gastar de más
MAX_SUGGESTIONS = 12
COVER_MIN_S, COVER_MAX_S = 0.5, 2.5  # debe coincidir con frame_portada_s en §11 del prompt: el gancho, ya completo
COVER_THUMB_WIDTH = 270  # lo que mide una portada en la cuadrícula de un perfil, aproximadamente
MIN_PACE, MAX_PACE = 130, 190  # palabras por minuto de una narración cómoda de seguir
UNSURE_WORD = 0.45  # probabilidad de Whisper por debajo de la cual una palabra quizá no se entiende
LONG_PAUSE, FROZEN = 1.2, 1.5  # segundos
CLIPPING_DB = -0.1
SEVERITIES = ("alta", "media", "baja")
KINDS = ("audio", "imagen", "texto", "comprension", "ritmo", "calidad", "portada")


def _plain(word: str) -> str:
    """Sin tildes, mayúsculas ni signos: «qué» y «que» se oyen igual."""
    text = unicodedata.normalize("NFKD", word).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", text)


NUMBER_WORDS = {"cero": "0", "un": "1", "uno": "1", "una": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6", "siete": "7",
                "ocho": "8", "nueve": "9", "diez": "10", "once": "11", "doce": "12", "quince": "15", "veinte": "20", "treinta": "30",
                "cuarenta": "40", "cincuenta": "50", "sesenta": "60", "setenta": "70", "ochenta": "80", "noventa": "90", "cien": "100", "mil": "1000"}


def _same_sound(said: str, heard: str) -> bool:
    """«cinco» y «5» o «ruge en» y «rujen» se oyen igual: Whisper escribe números y junta vocales."""
    a, b = said.replace(" ", ""), heard.replace(" ", "")
    if NUMBER_WORDS.get(said, said) == NUMBER_WORDS.get(heard, heard):
        return True
    # ge/je suenan igual, y una vocal doble se une al hablar.
    a, b = (re.sub(r"(.)\1+", r"\1", x.replace("ge", "je").replace("gi", "ji")) for x in (a, b))
    return bool(a) and a == b


def compare_speech(expected: str, heard: list[dict]) -> list[dict]:
    """Palabras del guion que Whisper no oyó igual, con su segundo. Es la mejor señal de una palabra
    mal pronunciada o cortada, aunque Whisper también se equivoca: es para revisarla, no una sentencia."""
    mine = [w for w in (_plain(x) for x in expected.split()) if w]
    theirs = [w for w in heard if _plain(w["word"])]
    keys = [_plain(w["word"]) for w in theirs]
    differences = []
    for op, a0, a1, b0, b1 in difflib.SequenceMatcher(None, mine, keys, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if _same_sound(" ".join(mine[a0:a1]), " ".join(keys[b0:b1])):
            continue
        at = theirs[min(b0, len(theirs) - 1)]["start"] if theirs else 0.0
        differences.append({"t": round(float(at), 1), "esperado": " ".join(mine[a0:a1]), "oido": " ".join(keys[b0:b1])})
    return differences


def _run_ffmpeg(*args: str) -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args], capture_output=True, text=True, timeout=900).stderr


def parse_video_filters(log: str) -> dict:
    """Congelados, negros y cortes del registro de ffmpeg."""
    frozen = [(float(a), float(b)) for a, b in zip(re.findall(r"freeze_start: ([\d.]+)", log), re.findall(r"freeze_duration: ([\d.]+)", log))]
    black = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:[\d.]+ black_duration:([\d.]+)", log)]
    return {"congelados": frozen, "negros": black, "cortes": len(re.findall(r"lavfi\.scene_score", log))}


def measure_video(video: Path) -> dict:
    log = _run_ffmpeg("-i", str(video), "-an", "-vf",
                      f"scale=270:-2,freezedetect=n=0.002:d={FROZEN},blackdetect=d=0.2:pix_th=0.06,"
                      "select='gt(scene,0.25)',metadata=print", "-f", "null", "-")
    return parse_video_filters(log)


def measure_audio(video: Path, voice: Path | None) -> dict:
    peak = re.search(r"max_volume: (-?[\d.]+) dB", _run_ffmpeg("-i", str(video), "-vn", "-af", "volumedetect", "-f", "null", "-"))
    pauses: list[tuple[float, float]] = []
    if voice and voice.exists():
        log = _run_ffmpeg("-i", str(voice), "-af", f"silencedetect=noise=-35dB:d={LONG_PAUSE}", "-f", "null", "-")
        ends = re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", log)
        pauses = [(round(float(e) - float(d), 1), float(d)) for e, d in ends]
    return {"pico_db": float(peak.group(1)) if peak else None, "pausas": pauses}


def transcribe_words(audio: Path) -> list[dict]:
    from faster_whisper import WhisperModel

    segments, _ = WhisperModel("small", device="cpu", compute_type="int8").transcribe(str(audio), word_timestamps=True)
    return [{"word": w.word.strip(), "start": float(w.start), "end": float(w.end), "prob": float(w.probability)}
            for s in segments for w in s.words]


def extract_frames(video: Path, out_dir: Path) -> list[dict]:
    """Un fotograma cada FRAME_EVERY segundos, de una sola pasada y empezando por el primero."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("t*.jpg"):
        old.unlink()
    fps = 24
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf",
                    f"select='not(mod(n,{fps * FRAME_EVERY}))',scale=540:-2", "-fps_mode", "passthrough", "-q:v", "4",
                    str(out_dir / "f%04d.jpg")], capture_output=True, timeout=900)
    frames = []
    for i, path in enumerate(sorted(out_dir.glob("f*.jpg"))):
        t = i * FRAME_EVERY
        target = out_dir / f"t{t:03d}.jpg"
        path.rename(target)
        frames.append({"t": t, "archivo": f"{FRAMES_DIR}/{target.name}"})
    return frames


def measured_suggestions(data: dict) -> list[dict]:
    """Lo que se sabe sin opinar: viene de las medidas, no de Claude."""
    found: list[dict] = []

    def add(gravedad: str, t: float, tipo: str, problema: str, correccion: str) -> None:
        found.append({"origen": "medicion", "gravedad": gravedad, "t": t, "tipo": tipo, "problema": problema, "correccion": correccion})

    for d in data.get("diferencias", []):
        if not d["esperado"] and len(d["oido"].split()) <= 1:
            continue  # Whisper añade muletillas: una palabra de más no es un error del guion
        add("alta" if len(d["esperado"].split()) <= 3 else "media", d["t"], "audio",
            f"El guion dice «{d['esperado'] or '(nada)'}», pero se transcribe «{d['oido'] or '(nada)'}». Quizá se pronuncia mal o se corta.",
            f"Regenera la frase del segundo {d['t']} con otra grafía o una pausa antes, sin recortar el inicio de «{d['esperado']}», hasta que "
            "la transcripción coincida con el guion, y rehaz sincronía, mezcla y render. Si la palabra ya se oye bien, descarta esta corrección.")
    for w in data.get("inseguras", []):
        add("baja", w["t"], "audio", f"«{w['word']}» se transcribe con poca seguridad ({w['prob']:.0%}).",
            f"Si en el segundo {w['t']} no se entiende, cambia esa frase hasta que la transcripción salga segura.")
    pace = data.get("palabras_minuto")
    if pace and not MIN_PACE <= pace <= MAX_PACE:
        add("media", 0, "ritmo", f"La narración va a {pace} palabras por minuto (cómodo: {MIN_PACE}–{MAX_PACE}).",
            "Ajusta la velocidad del TTS o el largo del guion, sin salir de la duración permitida.")
    for start, length in data.get("pausas", []):
        add("media", start, "ritmo", f"Hay {length:.1f} s sin voz.", "Acorta esa pausa o llénala con una transformación visual y un efecto de sonido.")
    peak = data.get("pico_db")
    if peak is not None and peak >= CLIPPING_DB:
        add("alta", 0, "audio", f"El audio llega a {peak} dB: puede saturar.", "Baja el pico con un limitador en mix.py y vuelve a mezclar.")
    for start, length in data.get("congelados", []):
        add("alta" if length >= 3 else "media", round(start, 1), "ritmo", f"La imagen se queda quieta {length:.1f} s.",
            "Añade movimiento de cámara o una transformación en ese tramo.")
    for start, length in data.get("negros", []):
        add("alta", round(start, 1), "imagen", f"Hay {length:.1f} s en negro.", "Corrige ese tramo de stage.html y vuelve a renderizar.")
    return found


REVIEW_PROMPT = """Eres el editor jefe de una cuenta de TikTok de videos explicativos para adultos. Revisa este video antes de publicarlo: sé exigente y concreto, como si tu cuenta dependiera de él.

Tienes un fotograma cada {cada} segundos, en orden (el primero es el segundo 0),. No puedes oír el video: de eso se ocupan otras medidas.

Serie: {serie}. Estilo que debe tener: {estilo}

Guion por escenas, con su tiempo:
{guion}

Medidas: {medidas}

Busca:
- Imagen: ¿cada objeto se reconoce sin narración? ¿lo que se ve corresponde a lo que se dice en ese segundo?
- Anatomía y formas: mira de cerca cada cuerpo u objeto. ¿Sus partes encajan (dedos con la palma, brazo con la mano), o hay huecos, piezas sueltas o líneas que se cortan? ¿Sus proporciones son creíbles? ¿Alguna figura atraviesa a otra o se atraviesa a sí misma, como unos brazos que se meten en el pecho? Es lo primero que delata un video barato: repórtalo como gravedad alta.
- Texto duplicado: ¿se lee la misma frase dos veces a la vez, por ejemplo en el cierre, con el subtítulo y el llamado a la acción? Repórtalo.
- Texto: subtítulos legibles, grandes y sin tapar al sujeto; faltas de ortografía; nada en las zonas que tapa la interfaz (230 px de arriba, 400 de abajo y 120 de la derecha sobre 1920×1080).
- Comprensión: ¿se entiende sin sonido? ¿algo confunde?
- Ritmo: tramos que se repiten o que aburren.
- Calidad: ¿parece hecho por profesionales o una presentación?
- Primer fotograma: ¿el gancho se lee y da ganas de seguir?

Reglas: solo problemas reales que veas, no gustos. Solo ves un fotograma cada {cada} segundos: no afirmes que falta un texto o una imagen porque no salga en ellos, puede estar entre dos. La palabra de color en los subtítulos es la que se está pronunciando, no un énfasis mal puesto. Las figuras pequeñas con media pantalla vacía sí son un problema. Como mucho {maximo}, de más a menos importante. «correccion» es una instrucción directa y concreta para quien lo va a arreglar en stage.html.

Responde solo con JSON, sin texto alrededor:
{{"resumen": "2 frases: qué tal está y qué es lo más urgente",
  "puntaje_visual": 7,
  "sugerencias": [{{"gravedad": "alta, media o baja", "t": 12, "tipo": "imagen, texto, comprension, ritmo o calidad", "problema": "...", "correccion": "..."}}]}}"""


def clean_suggestions(items: list) -> list[dict]:
    clean = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not str(item.get("problema") or "").strip():
            continue
        try:
            t = max(0.0, float(item.get("t") or 0))
        except (TypeError, ValueError):
            t = 0.0
        severity = str(item.get("gravedad") or "").strip().lower()
        kind = _plain(str(item.get("tipo") or ""))
        clean.append({"origen": "claude", "gravedad": severity if severity in SEVERITIES else "media", "t": t,
                      "tipo": kind if kind in KINDS else "calidad", "problema": str(item["problema"]).strip()[:500],
                      "correccion": str(item.get("correccion") or "").strip()[:600]})
    return clean[:MAX_SUGGESTIONS]


def _image_blocks(images: list[Path]) -> list[dict]:
    return [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(p.read_bytes()).decode()}}
            for p in images if p.exists()]


async def visual_review(out_dir: Path, frames: list[dict], measures: dict, request_serie: dict | None) -> dict:
    script = library.read_json(out_dir / "script.json")
    script = script if isinstance(script, dict) else {}
    timeline = library.read_json(out_dir / "timeline.json")
    scenes = timeline.get("scenes", []) if isinstance(timeline, dict) else timeline if isinstance(timeline, list) else []
    lines = []
    for i, scene in enumerate(script.get("escenas") or []):
        when = scenes[i] if i < len(scenes) and isinstance(scenes[i], dict) else {}
        lines.append(f"{when.get('start', '?')}–{when.get('end', '?')} s: {scene.get('vo', '')} [Visual: {scene.get('visual', '')}]")
    ficha = request_serie or {}
    prompt = REVIEW_PROMPT.format(cada=FRAME_EVERY, serie=ficha.get("nombre") or library.script_serie(script),
                                  estilo=ficha.get("estilo") or "(sin definir)",
                                  guion="\n".join(lines) or "(sin guion)", medidas=json.dumps(measures, ensure_ascii=False), maximo=MAX_SUGGESTIONS)
    content = _image_blocks([out_dir / f["archivo"] for f in frames])
    text, _ = await ask_claude([*content, {"type": "text", "text": prompt}])
    data = _extract_json(text, "{")
    if not isinstance(data, dict):
        raise RadarError("Claude no devolvió el formato esperado.")
    return data


COVER_PROMPT = """Valida la portada de un video de TikTok para adultos. Es el fotograma exacto que verá la gente. La primera imagen es esa portada a {ancho} px de ancho, como se ve en la cuadrícula de un perfil; la segunda, a tamaño completo.

Juzga la primera, la pequeña: si algo solo se aprecia en la grande, no cuenta. Sé estricto: la portada decide si alguien abre el video.

Responde solo con JSON, sin texto alrededor:
{{"texto": "las palabras que puedes leer sin esfuerzo en la miniatura, tal cual; vacío si no lees ninguna",
  "texto_se_lee": true,
  "sujeto_nitido": true,
  "subtitulos_encima": false,
  "problema": "qué falla, en una frase; vacío si está bien",
  "correccion": "instrucción concreta para arreglarlo; vacía si está bien"}}

«texto_se_lee» es true solo si el mensaje principal se lee entero y destaca del fondo. «sujeto_nitido» es false si el sujeto está borroso, lavado por halos o niebla, o no se reconoce. «subtitulos_encima» es true si, además del título, hay una línea de subtítulos o rótulos que compiten con él."""


def cover_verdict(publish: dict, gancho: str, raw: dict | None, generated: bool = True) -> dict:
    """El veredicto lo da el código: el segundo y el texto del gancho se comprueban; lo visual,
    con lo que Claude ve en la miniatura. Sin esa mirada, la portada queda «sin validar» y no «bien»."""
    problems: list[str] = []
    fixes = [f"Pon frame_portada_s entre {COVER_MIN_S} y {COVER_MAX_S} s, en un momento con el gancho completo y quieto y sin subtítulos encima."]
    t = None
    try:
        t = float(publish["frame_portada_s"])
    except (KeyError, TypeError, ValueError):
        problems.append("publish.json no indica el fotograma de la portada: lo elegiría TikTok.")
    if t is not None and not COVER_MIN_S <= t <= COVER_MAX_S:
        problems.append(f"La portada es el fotograma del segundo {t:g}, fuera del gancho ({COVER_MIN_S}–{COVER_MAX_S} s).")
    if not generated:
        problems.append("No se pudo sacar ese fotograma del video.")
    text = ""
    if raw:
        text = " ".join(str(raw.get("texto") or "").split())
        if raw.get("texto_se_lee") is not True:
            problems.append(f"El texto no se lee bien en miniatura{f' (se lee «{text}»)' if text else ''}.")
        elif gancho and not set(map(_plain, gancho.split())) <= set(map(_plain, text.split())):
            problems.append(f"La miniatura no muestra el gancho «{gancho}»: se lee «{text}».")
        if raw.get("sujeto_nitido") is not True:
            problems.append("El sujeto no se ve nítido en miniatura.")
        if raw.get("subtitulos_encima") is not False:
            problems.append("Hay subtítulos o rótulos que compiten con el título.")
        if str(raw.get("problema") or "").strip() and problems:
            problems.append(str(raw["problema"]).strip()[:300])
        if str(raw.get("correccion") or "").strip():
            fixes.append(str(raw["correccion"]).strip()[:300])
    ok = False if problems else (True if raw else None)
    return {"ok": ok, "t": t, "texto": text, "problemas": problems, "correccion": " ".join(fixes) if problems else ""}


def cover_suggestions(cover: dict) -> list[dict]:
    if cover.get("ok") is not False:
        return []
    return [{"origen": "medicion", "gravedad": "alta", "t": cover.get("t") or 0.0, "tipo": "portada",
             "problema": " ".join(cover["problemas"])[:500], "correccion": cover["correccion"][:600]}]


async def validate_cover(out_dir: Path) -> dict:
    """Mira la portada real, la que sale de final.mp4, al tamaño al que se decide si se abre."""
    publish, script = library.read_json(out_dir / "publish.json"), library.read_json(out_dir / "script.json")
    publish = publish if isinstance(publish, dict) else {}
    gancho = " ".join(str(script.get("texto_gancho") or "").split()) if isinstance(script, dict) else ""
    image = await asyncio.to_thread(library.cover, out_dir)
    if not image:
        return cover_verdict(publish, gancho, None, generated=False)
    (out_dir / FRAMES_DIR).mkdir(parents=True, exist_ok=True)
    mini = out_dir / FRAMES_DIR / "portada_mini.jpg"
    try:
        await asyncio.to_thread(subprocess.run, ["ffmpeg", "-y", "-loglevel", "error", "-i", str(image), "-vf", f"scale={COVER_THUMB_WIDTH}:-2",
                                                 "-q:v", "3", str(mini)], check=True, capture_output=True, timeout=60)
        text, _ = await ask_claude([*_image_blocks([mini, image]), {"type": "text", "text": COVER_PROMPT.format(ancho=COVER_THUMB_WIDTH)}])
        raw = _extract_json(text, "{")
        if not isinstance(raw, dict):
            raise RadarError("Claude no devolvió el formato esperado.")
    except Exception as exc:  # noqa: BLE001 — un fallo aquí no afecta al video: queda sin validar
        return {**cover_verdict(publish, gancho, None), "error": f"No se pudo mirar la portada: {exc}"[:300]}
    return cover_verdict(publish, gancho, raw)


def save(out_dir: Path, data: dict) -> dict:
    """Numera las sugerencias, las ordena por gravedad y las guarda."""
    order = {s: i for i, s in enumerate(SEVERITIES)}
    items = sorted(data.get("sugerencias", []), key=lambda s: (order.get(s.get("gravedad"), 1), s.get("t", 0)))
    frames = data.get("fotogramas", [])
    for i, item in enumerate(items):
        item["id"] = f"s{i + 1}"
        near = min(frames, key=lambda f: abs(f["t"] - item.get("t", 0)), default=None)
        item["fotograma"] = near["archivo"] if near else ""
    result = {**data, "sugerencias": items, "created_at": time.time()}
    (out_dir / FILE).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return result


async def review_video(out_dir: Path, serie: dict | None = None) -> dict:
    """Mide, transcribe y mira el video; si falla la parte de Claude, quedan las medidas."""
    video = out_dir / "final.mp4"
    voice = out_dir / "voice.wav"
    words = await asyncio.to_thread(transcribe_words, voice if voice.exists() else video)
    script = library.read_json(out_dir / "script.json")
    expected = " ".join(str(s.get("vo") or "") for s in (script or {}).get("escenas", []) if isinstance(s, dict)) if isinstance(script, dict) else ""
    audio = await asyncio.to_thread(measure_audio, video, voice)
    filters = await asyncio.to_thread(measure_video, video)
    frames = await asyncio.to_thread(extract_frames, video, out_dir / FRAMES_DIR)
    spoken = (words[-1]["end"] - words[0]["start"]) if len(words) > 1 else 0
    data = {
        **audio, **filters, "fotogramas": frames, "duracion": library.duration(video),
        "transcripcion": " ".join(w["word"] for w in words),
        "palabras_minuto": round(len(words) / spoken * 60) if spoken else None,
        "diferencias": compare_speech(expected, words) if expected else [],
        "inseguras": [{"t": round(w["start"], 1), "word": w["word"], "prob": round(w["prob"], 2)} for w in words if w["prob"] < UNSURE_WORD],
    }
    data["portada"] = await validate_cover(out_dir)
    suggestions = measured_suggestions(data) + cover_suggestions(data["portada"])
    measures = {k: data[k] for k in ("duracion", "palabras_minuto", "pico_db", "cortes", "congelados", "negros")}
    try:
        visual = await visual_review(out_dir, frames, measures, serie)
        data.update(resumen=str(visual.get("resumen") or "")[:600], puntaje_visual=visual.get("puntaje_visual"))
        suggestions += clean_suggestions(visual.get("sugerencias"))
    except Exception as exc:  # noqa: BLE001 — sin Claude, siguen valiendo las medidas
        data["error_vision"] = f"No se pudo revisar la imagen: {exc}"[:300]
    return save(out_dir, {**data, "sugerencias": suggestions})

