#!/usr/bin/env python3
"""Teatrito de Papel: lanza el agente que produce un video educativo de 60–65 s.

Carga prompts/system_prompt.md, reemplaza {{IDIOMA}}, {{TEMA}}, {{EDAD}},
{{SLUG}} y {{FORMATO}}, y lo usa como system_prompt del Claude Agent SDK.

Ejemplos:
    python run_agent.py --tema "Radiación de Hawking" --edad 6-9
    python run_agent.py --tema "Hawking radiation" --idioma en --formato horizontal
    python run_agent.py --check                 # verifica dependencias locales
    python run_agent.py --tema "Volcanes" --dry-run   # imprime el prompt final
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import shutil
import sys
import time
import unicodedata
from dataclasses import asdict, is_dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROMPT_PATH = ROOT / "prompts" / "system_prompt.md"

DEFAULT_MODEL = "claude-opus-5-5"
ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"]

KICKOFF = {
    "es": (
        "Produce el video completo sobre «{tema}» para niños de {edad} años, "
        "siguiendo el pipeline de principio a fin en output/{slug}/. "
        "No te detengas hasta tener final.mp4 y el QA hecho."
    ),
    "en": (
        "Produce the full video about “{tema}” for kids aged {edad}, "
        "following the pipeline end to end in output/{slug}/. "
        "Do not stop until final.mp4 exists and QA is done. "
        "(The system prompt is in Spanish; everything you produce must be in English.)"
    ),
}


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:60].rstrip("-") or "video"


def render_prompt(variables: dict[str, str]) -> str:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    for key, value in variables.items():
        prompt = prompt.replace("{{" + key + "}}", value)
    leftover = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", prompt)))
    if leftover:
        sys.exit(f"Variables sin reemplazar en el prompt: {', '.join(leftover)}")
    return prompt


# --------------------------------------------------------------------------- #
# --check: dependencias locales que el pipeline necesita
# --------------------------------------------------------------------------- #

def check_env() -> bool:
    binaries = {
        "ffmpeg": "render y mezcla",
        "ffprobe": "medir duraciones",
        "espeak-ng": "fonemas para Kokoro",
        "rubberband": "pitch de voz (opcional; hay fallback asetrate+atempo)",
    }
    modules = {
        "claude_agent_sdk": "este script",
        "kokoro": "voz principal",
        "piper": "voz fallback (opcional)",
        "soundfile": "WAV",
        "numpy": "audio / envolventes",
        "faster_whisper": "words.json",
        "playwright": "render de stage.html y audio.html",
    }
    optional = {"rubberband", "piper"}
    ok = True
    print("Binarios:")
    for name, why in binaries.items():
        found = shutil.which(name) is not None
        ok &= found or name in optional
        print(f"  {'✓' if found else ('·' if name in optional else '✗')} {name:<16} {why}")
    print("Módulos de Python:")
    for name, why in modules.items():
        found = importlib.util.find_spec(name) is not None
        ok &= found or name in optional
        print(f"  {'✓' if found else ('·' if name in optional else '✗')} {name:<16} {why}")
    fonts = sorted(p.name for p in (ROOT / "assets" / "fonts").glob("*.ttf"))
    print(f"Fuentes en assets/fonts/: {', '.join(fonts) or '(ninguna; el agente las descargará)'}")
    print("\nTodo listo." if ok else "\nFaltan dependencias: ejecuta ./setup.sh")
    return ok


# --------------------------------------------------------------------------- #
# Ejecución del agente
# --------------------------------------------------------------------------- #

def _short(value: object, limit: int = 140) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe_tool(name: str, tool_input: dict) -> str:
    for key in ("command", "file_path", "pattern", "url", "query", "description"):
        if key in tool_input:
            return f"{name}: {_short(tool_input[key])}"
    return f"{name}: {_short(tool_input)}"


def _jsonable(message: object) -> dict:
    data = asdict(message) if is_dataclass(message) else {"repr": repr(message)}
    return {"type": type(message).__name__, **data}


async def run(args: argparse.Namespace, system_prompt: str, out_dir: Path) -> int:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
        query,
    )

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=args.model,
        effort=args.effort,
        cwd=str(ROOT),
        allowed_tools=ALLOWED_TOOLS,
        permission_mode="acceptEdits",
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget_usd,
    )
    kickoff = KICKOFF[args.idioma].format(tema=args.tema, edad=args.edad, slug=args.slug)

    log_path = out_dir / "agent_log.jsonl"
    started = time.monotonic()
    exit_code = 1
    with log_path.open("a", encoding="utf-8") as log:
        async for message in query(prompt=kickoff, options=options):
            log.write(json.dumps(_jsonable(message), ensure_ascii=False, default=str) + "\n")
            log.flush()
            elapsed = f"[{int(time.monotonic() - started) // 60:02d}:{int(time.monotonic() - started) % 60:02d}]"
            if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        print(f"{elapsed} {block.text.strip()}", flush=True)
                    elif isinstance(block, ToolUseBlock):
                        print(f"{elapsed}   → {_describe_tool(block.name, block.input)}", flush=True)
            elif isinstance(message, ResultMessage):
                cost = f"${message.total_cost_usd:.2f}" if message.total_cost_usd is not None else "n/d"
                print(
                    f"\n{elapsed} Fin ({message.subtype}): {message.num_turns} turnos, costo {cost}.",
                    flush=True,
                )
                exit_code = 1 if message.is_error else 0

    final = out_dir / "final.mp4"
    print(f"Log: {log_path.relative_to(ROOT)}")
    print(f"Video: {final.relative_to(ROOT) if final.exists() else 'NO se generó final.mp4'}")
    return exit_code if final.exists() else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Agente «Teatrito de Papel» (videos de 60–65 s).")
    parser.add_argument("--tema", help="Tema del video, en el idioma del video.")
    parser.add_argument("--edad", default="6-9", help="Rango de edad (por defecto: 6-9).")
    parser.add_argument("--idioma", choices=["es", "en"], default="es")
    parser.add_argument("--formato", choices=["vertical", "horizontal"], default="vertical")
    parser.add_argument("--slug", help="Carpeta de salida (por defecto: tema + idioma).")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Modelo (por defecto: {DEFAULT_MODEL}).")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="high")
    parser.add_argument("--max-turns", type=int, default=400)
    parser.add_argument("--max-budget-usd", type=float, default=None, help="Tope de gasto del agente.")
    parser.add_argument("--dry-run", action="store_true", help="Solo imprime el system prompt final.")
    parser.add_argument("--check", action="store_true", help="Verifica dependencias y sale.")
    args = parser.parse_args()

    if args.check:
        return 0 if check_env() else 1
    if not args.tema:
        parser.error("--tema es obligatorio (salvo con --check)")

    args.slug = slugify(args.slug or f"{args.tema}-{args.idioma}")
    system_prompt = render_prompt(
        {
            "IDIOMA": args.idioma,
            "TEMA": args.tema,
            "EDAD": args.edad,
            "SLUG": args.slug,
            "FORMATO": args.formato,
        }
    )
    if args.dry_run:
        print(system_prompt)
        return 0

    out_dir = ROOT / "output" / args.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Tema: {args.tema} · {args.edad} años · {args.idioma} · {args.formato} → output/{args.slug}/")
    return asyncio.run(run(args, system_prompt, out_dir))


if __name__ == "__main__":
    sys.exit(main())
