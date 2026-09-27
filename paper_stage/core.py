"""Lógica compartida por la CLI (run_agent.py) y la aplicación web.

Carga prompts/system_prompt.md, reemplaza {{IDIOMA}}, {{TEMA}}, {{EDAD}},
{{SLUG}} y {{FORMATO}}, y ejecuta el agente con el Claude Agent SDK,
emitiendo eventos simples (texto, herramienta, resultado) para mostrar el progreso.
"""

from __future__ import annotations

import contextlib
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
# Frase final que Lía dice en cada video; se puede cambiar por video.
DEFAULT_CTA = {"es": "¡Sígueme para aprender más!", "en": "Follow me to learn more!"}
FORMATS = ["vertical", "horizontal"]
ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"]
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
# El agente corre en un solo turno: cuando lo cierra, el proceso de Claude Code termina
# y mata cualquier tarea en segundo plano (p. ej. un render a medias). Por eso todo va
# en primer plano, con tiempo de sobra para renderizar, y sin herramientas de "esperar".
AGENT_ENV = {
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "BASH_DEFAULT_TIMEOUT_MS": str(60 * 60 * 1000),
    "BASH_MAX_TIMEOUT_MS": str(2 * 60 * 60 * 1000),
}
DISALLOWED_TOOLS = ["ScheduleWakeup", "Monitor", "CronCreate", "RemoteTrigger"]
# Sugerir temas es una respuesta corta sin herramientas: basta un modelo rápido.
SUGGEST_MODEL = "claude-sonnet-5"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
AGE_RE = re.compile(r"^\d{1,2}(-\d{1,2})?$")

KICKOFF = {
    "es": (
        "Produce el video completo sobre «{tema}» para niños de {edad} años, "
        "siguiendo el pipeline de principio a fin en output/{slug}/. "
        "No te detengas hasta tener final.mp4 y los dos QA hechos."
    ),
    "en": (
        "Produce the full video about “{tema}” for kids aged {edad}, "
        "following the pipeline end to end in output/{slug}/. "
        "Do not stop until final.mp4 exists and both QA passes are done. "
        "(The system prompt is in Spanish; everything you produce must be in English.)"
    ),
}

RESUME = {
    "es": (
        "La ejecución anterior se interrumpió. Revisa qué hay ya en output/{slug}/, "
        "reutiliza lo que esté bien y continúa el pipeline desde el primer paso incompleto "
        "hasta tener qa_previo.md, final.mp4, publish.json y qa.md."
    ),
    "en": (
        "The previous run was interrupted. Check what already exists in output/{slug}/, "
        "reuse what is good and continue the pipeline from the first incomplete step "
        "until qa_previo.md, final.mp4, publish.json and qa.md exist."
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
    cta: str = ""
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
        self.cta = " ".join(self.cta.split()) or DEFAULT_CTA[self.idioma]
        if len(self.cta) > 80:
            raise ValueError("La frase final (CTA) debe tener 80 caracteres como máximo.")
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
            "CTA": self.cta or DEFAULT_CTA[self.idioma],
        }


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:60].rstrip("-") or "video"


def topic_key(tema: str, idioma: str) -> str:
    """Clave para detectar temas repetidos: sin tildes, mayúsculas ni signos, y por idioma
    (la versión en el otro idioma de un video es otro video). Temas parecidos pero no
    iguales tienen claves distintas."""
    text = unicodedata.normalize("NFKD", tema).encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", text)) + "|" + idioma


SUGGEST_PROMPT = {
    "es": "Propón {n} temas en español.",
    "en": "Propón {n} temas en inglés (el campo «tema» en inglés; el «gancho», en español).",
}


async def suggest_topics(idioma: str, edad: str, pista: str, used: list[str], n: int) -> list[dict]:
    """Pide a Claude n ideas de tema [{"tema", "gancho"}]. Quien llama descarta las repetidas."""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    lines = [
        "Eres el guionista de «Teatrito de Papel»: videos de 60 segundos con recortes de papel en los "
        f"que la niña Lía explica ciencia, naturaleza y curiosidades a niños de {edad} años.",
        SUGGEST_PROMPT[idioma].format(n=n),
        "Cada tema es una pregunta curiosa de 60 caracteres como máximo, que se pueda explicar y "
        "animar en un minuto. Que sean variados entre sí (distintas áreas).",
    ]
    if pista:
        lines.append(f"Deben tratar sobre: {pista}")
    if used:
        lines.append("Estos temas ya están hechos. Puedes proponer temas relacionados o parecidos, "
                     "pero nunca uno igual:\n" + "\n".join(f"- {t}" for t in used))
    lines.append('Responde solo con JSON, sin texto alrededor: [{"tema": "...", "gancho": "una frase corta (15 palabras como máximo) '
                 'sobre por qué le engancha a un niño"}]')
    options = ClaudeAgentOptions(model=SUGGEST_MODEL, tools=[], max_turns=1, setting_sources=[],
                                 cwd=str(ROOT))
    text = ""
    async for message in query(prompt="\n\n".join(lines), options=options):
        if isinstance(message, AssistantMessage):
            text += "".join(b.text for b in message.content if isinstance(b, TextBlock))
    match = re.search(r"\[.*\]", text, re.S)
    try:
        items = json.loads(match.group(0)) if match else []
    except ValueError:
        items = []
    return [{"tema": " ".join(str(i["tema"]).split()), "gancho": str(i.get("gancho") or "").strip()}
            for i in items if isinstance(i, dict) and str(i.get("tema") or "").strip()]


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


def _last_result(log_path: Path, session_id: str) -> dict | None:
    """Último ResultMessage de la sesión en agent_log.jsonl."""
    last = None
    with contextlib.suppress(OSError):
        with log_path.open(encoding="utf-8") as log:
            for line in log:
                if '"ResultMessage"' not in line:
                    continue
                with contextlib.suppress(ValueError):
                    row = json.loads(line)
                    if row.get("type") == "ResultMessage" and row.get("session_id") == session_id:
                        last = row
    return last


def _includes(result: dict, earlier: dict) -> bool:
    """¿El acumulado de `result` ya contiene todo lo que informó `earlier`?"""
    if (result.get("total_cost_usd") or 0) < (earlier.get("total_cost_usd") or 0):
        return False
    usage = result.get("model_usage") or {}
    for model, old in (earlier.get("model_usage") or {}).items():
        new = usage.get(model)
        if not new or any((new.get(k) or 0) < (old.get(k) or 0)
                          for k in ("inputTokens", "outputTokens", "cacheReadInputTokens")):
            return False
    return True


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
    cost_usd es lo gastado desde el resultado anterior de la sesión. Claude Code informa
    un acumulado: repite el resultado al cerrar y, al reanudar, a veces arrastra lo
    gastado en ejecuciones anteriores; sumar sus cifras tal cual cuenta todo varias veces.
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
        disallowed_tools=DISALLOWED_TOOLS,
        env=AGENT_ENV,
        permission_mode="acceptEdits",
        max_turns=req.max_turns,
        max_budget_usd=req.max_budget_usd,
        resume=resume_session,
        stderr=on_stderr,
        # Al leer fotogramas y portadas, la imagen llega en base64 en un solo
        # mensaje y supera con facilidad el límite de 1 MB del SDK.
        max_buffer_size=MAX_MESSAGE_BYTES,
    )
    prompt = kickoff_prompt(req, resume=continuing or resume_session is not None)

    req.out_dir.mkdir(parents=True, exist_ok=True)
    log_path = req.out_dir / "agent_log.jsonl"
    previous = _last_result(log_path, resume_session) if resume_session else None
    reported: float | None = None  # acumulado ya contabilizado en esta ejecución
    session_seen = False
    # async for no cierra el generador si el bucle se interrumpe (p. ej. al cancelar);
    # aclose() garantiza que el SDK termine el proceso de Claude Code.
    messages = query(prompt=prompt, options=options)
    try:
        with log_path.open("a", encoding="utf-8") as log:
            async for message in messages:
                row = _jsonable(message)
                log.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
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
                    spent = None
                    if message.total_cost_usd is not None:
                        if reported is None:
                            carried = previous is not None and _includes(row, previous)
                            reported = previous["total_cost_usd"] if carried else 0.0
                        spent = max(message.total_cost_usd - reported, 0.0)
                        reported = max(reported, message.total_cost_usd)
                    yield {
                        "kind": "result",
                        "ok": not message.is_error,
                        "subtype": message.subtype,
                        "turns": message.num_turns,
                        "cost_usd": spent,
                        "session_id": message.session_id,
                        "text": (message.result or "").strip(),
                    }
    finally:
        await messages.aclose()
