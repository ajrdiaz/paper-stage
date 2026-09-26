"""Agente de demostración: simula el pipeline sin llamar a Claude ni gastar créditos.

Sirve para probar la aplicación web (PAPER_STAGE_DEMO=1) y para los tests. Escribe
archivos de ejemplo en output/<slug>/ con la misma estructura que produce el agente real:
un stage.html determinista (con window.renderAt), un mix.wav de caja musical y, si hay
ffmpeg, un final.mp4 corto de marcador.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from typing import AsyncIterator, Callable

from .core import VideoRequest

SPEED = float(os.environ.get("PAPER_STAGE_DEMO_DELAY", "1.2"))

LINES = {
    "es": [
        "¿Sabías que {tema} esconde un secreto sorprendente?",
        "Imagínalo como algo que ves todos los días… y al final vas a ver lo más raro.",
        "Y aquí viene lo más raro: ¡nadie lo esperaba!",
        "Dos pedacitos de papel nos lo van a explicar, paso a paso.",
        "¿Qué crees que pasa? ¡Dilo en voz alta!",
        "¡Exacto! Y ese era el secreto del principio.",
        "Ahora ya sabes algo nuevo. ¿Qué tema quieren para mañana?",
    ],
    "en": [
        "Did you know that {tema} hides an amazing secret?",
        "Picture something you see every day… and at the end you'll see the strangest part.",
        "And here comes the weirdest part: nobody expected it!",
        "Two little paper friends will explain it, step by step.",
        "What do you think happens? Say it out loud!",
        "Exactly! And that was the secret from the start.",
        "Now you know something new. What topic should we do tomorrow?",
    ],
}
TITLES = ["La idea", "¿Qué es?", "El detalle clave", "El corazón", "Mini-quiz", "La revelación", "De vuelta"]
SCENE_LEN = [7, 9, 10, 11, 9, 10, 6.5]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _silence(path: Path, seconds: float, rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(seconds * rate))


def _music_box(path: Path, seconds: float, rate: int = 22050) -> None:
    notes = [72, 76, 79, 84, 79, 76, 74, 77, 81, 86, 81, 77]  # Do mayor, arpegios
    beat = 60 / 104
    frames = bytearray()
    for i in range(int(seconds * rate)):
        t = i / rate
        n = int(t / beat)
        dt = t - n * beat
        f = 440 * 2 ** ((notes[n % len(notes)] - 69) / 12)
        env = math.exp(-dt * 5)
        s = env * (math.sin(2 * math.pi * f * t) + 0.3 * math.sin(4 * math.pi * f * t)) * 0.25
        frames += struct.pack("<h", int(max(-1, min(1, s)) * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def _timeline() -> list[dict]:
    t, scenes = 0.0, []
    for i, length in enumerate(SCENE_LEN):
        scenes.append({"n": i + 1, "start": round(t, 2), "end": round(t + length, 2)})
        t += length
    return scenes


def _words(lines: list[str], timeline: list[dict]) -> list[dict]:
    words = []
    for line, scene in zip(lines, timeline):
        tokens = line.split()
        start, end = scene["start"] + 0.2, scene["end"] - 0.6
        step = (end - start) / len(tokens)
        for i, tok in enumerate(tokens):
            words.append({"word": tok, "start": round(start + i * step, 2),
                          "end": round(start + (i + 0.85) * step, 2)})
    return words


def _mouth(words: list[dict], duration: float) -> list[float]:
    values = []
    for i in range(int(duration * 24)):
        t = i / 24
        speaking = any(w["start"] <= t <= w["end"] for w in words)
        values.append(round(abs(math.sin(t * 13)) * 0.9, 2) if speaking else 0)
    return values


def _stage_html(req: VideoRequest, title: str, words: list[dict], mouth: list[float],
                timeline: list[dict], duration: float) -> str:
    w, h = (1080, 1920) if req.formato == "vertical" else (1920, 1080)
    data = json.dumps({"words": words, "mouth": mouth, "timeline": timeline}, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="{req.idioma}"><head><meta charset="utf-8"><title>{title}</title>
<style>
html,body{{margin:0;background:#1E2A4F;overflow:hidden}}
svg{{display:block}}
#cap{{position:absolute;left:0;right:0;top:{int(h*0.79)}px;text-align:center;font:600 58px 'Fredoka',system-ui,sans-serif;color:#F3E9D2;padding:0 90px}}
#cap b{{color:#E9B949;display:inline-block;transform:translateY(-6px)}}
</style></head><body>
<svg id="s" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
 <defs><filter id="torn"><feTurbulence baseFrequency="0.04" numOctaves="2" seed="3"/><feDisplacementMap in="SourceGraphic" scale="6"/></filter></defs>
 <rect width="{w}" height="{h}" fill="#1E2A4F"/>
 <g id="stage" transform="translate({(w-1000)//2},230)">
  <rect width="1000" height="1250" fill="#C9A57A" filter="url(#torn)"/>
  <rect id="bg" x="30" y="30" width="940" height="1190" fill="#E9B949"/>
  <circle id="sun" cx="500" cy="380" r="110" fill="#E8736B" stroke="#fff" stroke-width="4" filter="url(#torn)"/>
  <rect x="30" y="1060" width="940" height="160" fill="#8B5E3C"/>
  <path d="M30 30h170c-30 400 20 800-40 1190H30z" fill="#E8736B" stroke="#fff" stroke-width="4" filter="url(#torn)"/>
  <path d="M970 30H800c30 400-20 800 40 1190h130z" fill="#E8736B" stroke="#fff" stroke-width="4" filter="url(#torn)"/>
  <g id="lia" transform="translate(500,760)">
   <path d="M-120 300 q0-160 120-170 q120 10 120 170z" fill="#3FA796" stroke="#fff" stroke-width="4"/>
   <circle r="95" fill="#F3D5B5" stroke="#fff" stroke-width="4"/>
   <path d="M-105 10 q0-120 105-120 q105 0 105 120 l-20 0 q-10-70-85-80 q-75 10-85 80z" fill="#222"/>
   <rect x="40" y="-85" width="36" height="14" rx="6" fill="#E9B949"/>
   <circle cx="-35" cy="5" r="8" fill="#222"/><circle cx="35" cy="5" r="8" fill="#222"/>
   <circle cx="-60" cy="35" r="16" fill="#E8736B" opacity=".6"/><circle cx="60" cy="35" r="16" fill="#E8736B" opacity=".6"/>
   <ellipse id="mouth" cx="0" cy="50" rx="10" ry="4" fill="#7a2b2b"/>
  </g>
  <text id="title" x="500" y="160" text-anchor="middle" font-family="Gaegu,'Patrick Hand',cursive" font-size="92" fill="#1E2A4F">{title}</text>
 </g>
</svg>
<div id="cap"></div>
<script>
const D={data};
window.DURATION={duration};
function rnd(s){{return function(){{s|=0;s=s+0x6D2B79F5|0;let t=Math.imul(s^s>>>15,1|s);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}}}
window.renderAt=function(t){{
  const tm=Math.floor(t*12)/12, boil=Math.floor(t*8)%3, r=rnd(boil+1);
  const scene=D.timeline.findIndex(s=>t>=s.start&&t<s.end);
  const night=scene>=1&&scene<=5;
  document.getElementById('bg').setAttribute('fill',night?'#4B3B7A':'#E9B949');
  const sun=document.getElementById('sun');
  sun.setAttribute('cx',500+Math.sin(tm*0.6)*220);
  sun.setAttribute('r',110+Math.sin(tm*1.3)*18+(r()-.5)*3);
  document.getElementById('lia').setAttribute('transform',`translate(${{500+(r()-.5)*3}},${{760+Math.sin(tm*2)*6}})`);
  const m=D.mouth[Math.min(D.mouth.length-1,Math.floor(t*24))]||0;
  const mouth=document.getElementById('mouth');
  mouth.setAttribute('rx',10+m*8);mouth.setAttribute('ry',4+m*18);
  document.getElementById('title').style.opacity=t<7?1:0;
  const i=D.words.findIndex(w=>t>=w.start&&t<=w.end+0.25);
  if(i<0){{document.getElementById('cap').innerHTML='';return}}
  const a=Math.max(0,i-4), b=Math.min(D.words.length,a+9);
  document.getElementById('cap').innerHTML=D.words.slice(a,b).map((w,k)=>a+k===i?`<b>${{w.word}}</b>`:w.word).join(' ');
}};
window.renderAt(0);
</script></body></html>"""


async def run_demo(
    req: VideoRequest,
    *,
    resume_session: str | None = None,
    continuing: bool = False,
    on_stderr: Callable[[str], None] | None = None,
) -> AsyncIterator[dict]:
    out = req.out_dir
    out.mkdir(parents=True, exist_ok=True)
    lines = [line.format(tema=req.tema) for line in LINES[req.idioma]]
    timeline = _timeline()
    duration = timeline[-1]["end"]
    words = _words(lines, timeline)
    title = req.tema[:40]

    async def step(tool: str, detail: str, action: Callable[[], None] | None = None,
                   note: str | None = None) -> AsyncIterator[dict]:
        if note:
            yield {"kind": "text", "text": note}
        yield {"kind": "tool", "tool": tool, "detail": detail}
        await asyncio.sleep(SPEED)
        if action:
            await asyncio.to_thread(action)

    yield {"kind": "session", "session_id": resume_session or f"demo-{req.slug}"}
    yield {"kind": "text", "text": "Modo demostración: no se llama a Claude y los datos son de ejemplo."}

    plan = [
        ("WebSearch", f"{req.tema} datos para niños", lambda: _write(out / "research.md",
            f"# Investigación: {req.tema}\n\n1. Dato de ejemplo (modo demo).\n   Fuente: —\n"),
         "Investigo el tema y elijo el dato más sorprendente como gancho."),
        ("Write", "bible.md", lambda: _write(out / "bible.md",
            "# Biblia visual\n\n## ESTILO\nRecortes de papel…\n\n## PERSONAJE PRINCIPAL\nLía…\n"), None),
        ("Write", "script.json", lambda: _write(out / "script.json", json.dumps({
            "idioma": req.idioma, "formato": req.formato, "titulo_crayon": title,
            "gancho": lines[0], "bucle_abierto": "lo más raro, al final",
            "escenas": [{"n": i + 1, "titulo": TITLES[i], "vo": line,
                         "visual": "Escena de ejemplo", "transition": "El papel se dobla…"}
                        for i, line in enumerate(lines)],
        }, ensure_ascii=False, indent=2)), "Escribo el guion en 7 escenas."),
        ("Bash", "Generar la voz con Kokoro (escenas 1–7)", lambda: [
            _silence(out / "audio" / f"scene_{i + 1:02d}.wav", length - 0.5)
            for i, length in enumerate(SCENE_LEN)], None),
        ("Write", "timeline.json", lambda: _write(out / "timeline.json",
            json.dumps({"duration": duration, "scenes": timeline}, indent=2)), None),
        ("Bash", "faster-whisper → words.json y envolvente → mouth.json", lambda: (
            _write(out / "words.json", json.dumps(words, ensure_ascii=False)),
            _write(out / "mouth.json", json.dumps(_mouth(words, duration))),
        ), None),
        ("Write", "stage.html", lambda: _write(out / "stage.html", _stage_html(
            req, title, words, _mouth(words, duration), timeline, duration)),
         "Animo el teatrito en stage.html con reloj determinista."),
        ("Bash", "Sintetizar música y efectos, mezclar a −14 LUFS",
         lambda: _music_box(out / "mix.wav", duration), None),
        ("Bash", "Render con Playwright + ffmpeg → final.mp4", lambda: _fake_mp4(out, duration, req), None),
        ("Write", "qa.md", lambda: _write(out / "qa.md",
            "# QA (demo)\n\n- [x] Duración entre 60.0 y 65.0 s\n- [x] Subtítulos debajo del escenario\n"
            "- [ ] Voz real (el modo demo usa silencio)\n"), None),
        ("Write", "publish.json", lambda: _write(out / "publish.json", json.dumps({
            "idioma": req.idioma,
            "titulos": [f"¿Qué esconde {req.tema}?", f"{req.tema} en 60 segundos", "Lía te lo explica"],
            "descripcion": "Video de ejemplo generado en modo demostración.",
            "hashtags": ["#shorts", "#cienciaparaniños", "#teatritodepapel"],
            "texto_portada": title, "frame_portada_s": 1.5, "hecho_para_ninos": True,
            "serie": "Teatrito de Papel",
            "ideas_siguientes": ["Los volcanes", "Por qué el cielo es azul", "Cómo duermen los delfines"],
        }, ensure_ascii=False, indent=2)), None),
    ]
    for tool, detail, action, note in plan:
        async for event in step(tool, detail, action, note):
            yield event

    final = out / "final.mp4"
    yield {
        "kind": "result", "ok": True, "subtype": "success", "turns": len(plan) * 2,
        "cost_usd": 0.0, "session_id": resume_session or f"demo-{req.slug}",
        "text": f"Duración: {duration:.1f} s · Gancho: {lines[0]} · QA: voz de ejemplo."
        if final.exists() else "Modo demo sin ffmpeg: no se generó final.mp4.",
    }


def _fake_mp4(out: Path, duration: float, req: VideoRequest) -> None:
    if not shutil.which("ffmpeg"):
        return
    size = "540x960" if req.formato == "vertical" else "960x540"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=0xE9B949:s={size}:r=24:d={duration}",
         "-i", str(out / "mix.wav"),
         "-vf", "drawbox=x=iw*0.08:y=ih*0.12:w=iw*0.84:h=ih*0.62:color=0xE8736B@1:t=fill,"
                "drawbox=x=iw*0.3+mod(t*40\\,iw*0.3):y=ih*0.3:w=iw*0.1:h=iw*0.1:color=0x3FA796@1:t=fill",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(out / "final.mp4")],
        capture_output=True, timeout=300,
    )
