"""Lógica compartida por la CLI (run_agent.py) y la aplicación web.

Carga prompts/system_prompt.md, reemplaza {{IDIOMA}}, {{TEMA}}, {{EDAD}},
{{SLUG}} y {{FORMATO}}, y ejecuta el agente con el Claude Agent SDK,
emitiendo eventos simples (texto, herramienta, resultado) para mostrar el progreso.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import unicodedata
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = ROOT / "prompts" / "system_prompt.md"
# El prompt le pide al agente trabajar en output/<slug>/ relativo a ROOT; cambiar estas
# rutas solo tiene sentido para pruebas y para el agente de demostración.
OUTPUT_DIR = Path(os.environ.get("PAPER_STAGE_OUTPUT", ROOT / "output"))
DATA_DIR = Path(os.environ.get("PAPER_STAGE_DATA", ROOT / "data"))
FONTS_DIR = ROOT / "assets" / "fonts"

DEFAULT_MODEL = "claude-opus-5-5"
MODELS = ["claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
LANGUAGES = ["es", "en"]
FORMATS = ["vertical", "horizontal"]
ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"]

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
AGE_RE = re.compile(r"^\d{1,2}(-\d{1,2})?$")

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

RESUME = {
    "es": (
        "La ejecución anterior se interrumpió. Revisa qué hay ya en output/{slug}/, "
        "reutiliza lo que esté bien y continúa el pipeline desde el primer paso incompleto "
        "hasta tener final.mp4, qa.md y publish.json."
    ),
    "en": (
        "The previous run was interrupted. Check what already exists in output/{slug}/, "
        "reuse what is good and continue the pipeline from the first incomplete step "
        "until final.mp4, qa.md and publish.json exist."
    ),
}


@dataclass
class VideoRequest:
    tema: str
    edad: str = "6-9"
    idioma: str = "es"
    formato: str = "vertical"
    slug: str = ""
    model: str = DEFAULT_MODEL
    effort: str = "high"
    max_turns: int = 400
    max_budget_usd: float | None = None
    extra: dict = field(default_factory=dict)

    def validate(self) -> None:
        self.tema = " ".join(self.tema.split())
        if not 2 <= len(self.tema) <= 200:
            raise ValueError("El tema debe tener entre 2 y 200 caracteres.")
        if not AGE_RE.match(self.edad):
            raise ValueError("La edad debe ser un número o un rango como 6-9.")
        if self.idioma not in LANGUAGES:
            raise ValueError(f"Idioma no válido: {self.idioma}")
        if self.formato not in FORMATS:
            raise ValueError(f"Formato no válido: {self.formato}")
        if self.effort not in EFFORTS:
            raise ValueError(f"Esfuerzo no válido: {self.effort}")
        if not re.match(r"^[a-z0-9][a-z0-9.-]{1,63}$", self.model):
            raise ValueError(f"Modelo no válido: {self.model}")
        if not 10 <= int(self.max_turns) <= 2000:
            raise ValueError("max_turns debe estar entre 10 y 2000.")
        if self.max_budget_usd is not None and not 0.5 <= float(self.max_budget_usd) <= 1000:
            raise ValueError("El tope de gasto debe estar entre 0.5 y 1000 USD.")
        self.slug = slugify(self.slug or f"{self.tema}-{self.idioma}")

    @property
    def out_dir(self) -> Path:
        return OUTPUT_DIR / self.slug

    def variables(self) -> dict[str, str]:
        return {
            "IDIOMA": self.idioma,
            "TEMA": self.tema,
            "EDAD": self.edad,
            "SLUG": self.slug,
            "FORMATO": self.formato,
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
        raise ValueError(f"Variables sin reemplazar en el prompt: {', '.join(leftover)}")
    return prompt


def kickoff_prompt(req: VideoRequest, resume: bool = False) -> str:
    template = (RESUME if resume else KICKOFF)[req.idioma]
    return template.format(tema=req.tema, edad=req.edad, slug=req.slug)


# --------------------------------------------------------------------------- #
# Dependencias locales
# --------------------------------------------------------------------------- #

BINARIES = {
    "ffmpeg": ("render y mezcla", False),
    "ffprobe": ("medir duraciones", False),
    "espeak-ng": ("fonemas para Kokoro", False),
    "rubberband": ("pitch de voz (hay fallback asetrate+atempo)", True),
}
MODULES = {
    "claude_agent_sdk": ("el agente", False),
    "kokoro": ("voz principal", False),
    "piper": ("voz fallback", True),
    "soundfile": ("WAV", False),
    "numpy": ("audio / envolventes", False),
    "faster_whisper": ("words.json", False),
    "playwright": ("render de stage.html y audio.html", False),
}


def check_env() -> dict:
    items = []
    for name, (why, optional) in BINARIES.items():
        items.append({"kind": "bin", "name": name, "why": why, "optional": optional,
                      "ok": shutil.which(name) is not None})
    for name, (why, optional) in MODULES.items():
        items.append({"kind": "module", "name": name, "why": why, "optional": optional,
                      "ok": importlib.util.find_spec(name) is not None})
    fonts = sorted(p.name for p in FONTS_DIR.glob("*.ttf"))
    ok = all(i["ok"] or i["optional"] for i in items)
    return {"ok": ok, "items": items, "fonts": fonts}


# --------------------------------------------------------------------------- #
# Ejecución del agente
# --------------------------------------------------------------------------- #

def _short(value: object, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_tool(name: str, tool_input: dict) -> str:
    for key in ("description", "command", "file_path", "pattern", "url", "query"):
        if key in tool_input:
            return _short(tool_input[key])
    return _short(tool_input)


def _jsonable(message: object) -> dict:
    data = asdict(message) if is_dataclass(message) else {"repr": repr(message)}
    return {"type": type(message).__name__, **data}


async def run_agent(
    req: VideoRequest,
    *,
    resume_session: str | None = None,
    continuing: bool = False,
    on_stderr: Callable[[str], None] | None = None,
) -> AsyncIterator[dict]:
    """Ejecuta el agente y produce eventos simples.

    Tipos de evento:
      {"kind": "session", "session_id": ...}
      {"kind": "text", "text": ...}
      {"kind": "tool", "tool": ..., "detail": ...}
      {"kind": "result", "ok": bool, "subtype": ..., "turns": int, "cost_usd": float|None,
       "session_id": ..., "text": ...}
    Además escribe el registro completo en output/<slug>/agent_log.jsonl.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolUseBlock,
        query,
    )

    options = ClaudeAgentOptions(
        system_prompt=render_prompt(req.variables()),
        model=req.model,
        effort=req.effort,
        cwd=str(ROOT),
        allowed_tools=ALLOWED_TOOLS,
        permission_mode="acceptEdits",
        max_turns=req.max_turns,
        max_budget_usd=req.max_budget_usd,
        resume=resume_session,
        stderr=on_stderr,
    )
    prompt = kickoff_prompt(req, resume=continuing or resume_session is not None)

    req.out_dir.mkdir(parents=True, exist_ok=True)
    session_seen = False
    # async for no cierra el generador si el bucle se interrumpe (p. ej. al cancelar);
    # aclose() garantiza que el SDK termine el proceso de Claude Code.
    messages = query(prompt=prompt, options=options)
    try:
        with (req.out_dir / "agent_log.jsonl").open("a", encoding="utf-8") as log:
            async for message in messages:
                log.write(json.dumps(_jsonable(message), ensure_ascii=False, default=str) + "\n")
                log.flush()
                if isinstance(message, SystemMessage) and not session_seen:
                    session_id = message.data.get("session_id")
                    if session_id:
                        session_seen = True
                        yield {"kind": "session", "session_id": session_id}
                elif isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            yield {"kind": "text", "text": block.text.strip()}
                        elif isinstance(block, ToolUseBlock):
                            yield {"kind": "tool", "tool": block.name,
                                   "detail": describe_tool(block.name, block.input)}
                elif isinstance(message, ResultMessage):
                    yield {
                        "kind": "result",
                        "ok": not message.is_error,
                        "subtype": message.subtype,
                        "turns": message.num_turns,
                        "cost_usd": message.total_cost_usd,
                        "session_id": message.session_id,
                        "text": (message.result or "").strip(),
                    }
    finally:
        await messages.aclose()
