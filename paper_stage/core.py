"""Lógica compartida por la CLI (run_agent.py) y la aplicación web.

Define los personajes (Character) y las peticiones de video (VideoRequest), carga
prompts/system_prompt.md, reemplaza sus variables {{X}} y ejecuta el agente con el
Claude Agent SDK, emitiendo eventos simples (texto, herramienta, resultado).
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = ROOT / "prompts" / "system_prompt.md"
# El prompt le pide al agente trabajar en output/<slug>/ relativo a ROOT; cambiar estas
# rutas solo tiene sentido para pruebas y para el agente de demostración.
OUTPUT_DIR = Path(os.environ.get("PAPER_STAGE_OUTPUT", ROOT / "output"))
DATA_DIR = Path(os.environ.get("PAPER_STAGE_DATA", ROOT / "data"))
FONTS_DIR = ROOT / "assets" / "fonts"
# Librerías de animación que el agente puede incrustar en stage.html (las baja setup.sh).
VENDOR_DIR = ROOT / "assets" / "vendor"
VENDOR_FILES = ["gsap.min.js", "MorphSVGPlugin.min.js", "CustomEase.min.js"]

DEFAULT_MODEL = "claude-opus-5-5"
MODELS = ["claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
LANGUAGES = ["es", "en"]
LANGUAGE_NAMES = {"es": "español", "en": "inglés"}
# Frase final por defecto si el personaje no tiene una; se puede cambiar por video.
DEFAULT_CTA = {"es": "¡Sígueme para aprender más!", "en": "Follow me to learn more!"}
# Voces de Kokoro-82M por idioma del video (la primera letra del ID es su lang_code;
# "f"/"m" en la segunda, femenina o masculina). En inglés, solo las americanas (§0 del prompt).
VOICES = {
    "es": ["ef_dora", "em_alex", "em_santa"],
    "en": ["af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore", "af_nicole",
           "af_nova", "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric",
           "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa"],
}
DEFAULT_VOICES = {"femenina": {"es": "ef_dora", "en": "af_heart"},
                  "masculina": {"es": "em_alex", "en": "am_michael"}}
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


def agent_env() -> dict[str, str]:
    """AGENT_ENV más el entorno virtual de la app al frente del PATH: el agente llama a
    `python3` y `playwright` a secas, y sin esto usaría los del sistema (sin Kokoro,
    Whisper ni Playwright) si la app no se lanzó con el venv activado (p. ej. run.sh)."""
    env = dict(AGENT_ENV)
    if sys.prefix != sys.base_prefix:
        bin_dir = str(Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin"))
        env["VIRTUAL_ENV"] = sys.prefix
        env["PATH"] = os.pathsep.join([bin_dir, os.environ.get("PATH", "")])
    return env
# Sugerir temas es una respuesta corta sin herramientas: basta un modelo rápido.
SUGGEST_MODEL = "claude-sonnet-5"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
AGE_RE = re.compile(r"^\d{1,2}(-\d{1,2})?$")
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

KICKOFF = {
    "es": (
        "Produce el video completo de {personaje} sobre «{tema}» para niños de {edad} años, "
        "siguiendo el pipeline de principio a fin en output/{slug}/. "
        "No te detengas hasta tener final.mp4 y los dos QA hechos."
    ),
    "en": (
        "Produce {personaje}'s full video about “{tema}” for kids aged {edad}, "
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


def _clean(text: object) -> str:
    return " ".join(str(text or "").split())


def _check_len(label: str, text: str, low: int, high: int) -> None:
    if not low <= len(text) <= high:
        raise ValueError(f"{label} debe tener entre {low} y {high} caracteres.")


@dataclass
class Character:
    """Personaje que presenta una serie. La técnica (recortes de papel) es común a todos;
    el personaje fija su aspecto, su voz, su escenario, su paleta y el nicho de sus videos."""

    nombre: str
    nicho: str
    apariencia: str
    personalidad: str
    escenario: str
    paleta: list[str]
    idioma: str = "es"  # idioma principal: el de sus videos por defecto
    edad: str = "6-9"
    voces: dict = field(default_factory=lambda: dict(DEFAULT_VOICES["femenina"]))
    voz_estilo: str = ""
    serie: str = ""
    cta: dict = field(default_factory=dict)  # frase final por idioma
    id: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Character":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        for name in ("nombre", "nicho", "apariencia", "personalidad", "escenario", "voz_estilo", "serie"):
            setattr(self, name, _clean(getattr(self, name)))
        _check_len("El nombre", self.nombre, 2, 30)
        _check_len("El nicho", self.nicho, 3, 120)
        _check_len("La apariencia", self.apariencia, 20, 800)
        _check_len("La personalidad", self.personalidad, 10, 400)
        _check_len("El escenario", self.escenario, 10, 400)
        _check_len("El estilo de voz", self.voz_estilo, 0, 200)
        self.serie = self.serie or self.nombre
        _check_len("El nombre de la serie", self.serie, 2, 40)
        if self.idioma not in LANGUAGES:
            raise ValueError(f"Idioma no válido: {self.idioma}")
        if not AGE_RE.match(str(self.edad)):
            raise ValueError("La edad debe ser un número o un rango como 6-9.")
        if not isinstance(self.paleta, list) or not 3 <= len(self.paleta) <= 7 \
                or not all(isinstance(c, str) and HEX_RE.match(c) for c in self.paleta):
            raise ValueError("La paleta debe tener entre 3 y 7 colores en formato #RRGGBB.")
        self.paleta = [c.upper() for c in self.paleta]
        voces = self.voces if isinstance(self.voces, dict) else {}
        for lang in LANGUAGES:
            if voces.get(lang) not in VOICES[lang]:
                raise ValueError(f"Voz no válida para {LANGUAGE_NAMES[lang]}: {voces.get(lang)}")
        self.voces = {lang: voces[lang] for lang in LANGUAGES}
        cta = self.cta if isinstance(self.cta, dict) else {}
        self.cta = {lang: _clean(cta.get(lang)) for lang in LANGUAGES if _clean(cta.get(lang))}
        if any(len(c) > 80 for c in self.cta.values()):
            raise ValueError("La frase final (CTA) debe tener 80 caracteres como máximo.")

    def cta_for(self, idioma: str) -> str:
        return self.cta.get(idioma) or DEFAULT_CTA[idioma]

    @property
    def hashtag(self) -> str:
        return "#" + slugify(self.serie).replace("-", "")


# El personaje original de la serie. Los videos y trabajos anteriores a los personajes
# (sin "personaje" en script.json o en la petición) son suyos.
LIA = Character(
    id="lia",
    nombre="Lía",
    nicho="Ciencia y curiosidades del mundo",
    apariencia=(
        "Una niña curiosa con pelo negro corte bob, un gancho amarillo en el pelo, ojos de punto, "
        "una boquita redonda en \"o\", cachetes rosados circulares y un suéter teal con cuello blanco. "
        "Sostiene un crayón azul y señala con él lo que explica."
    ),
    personalidad=(
        "Curiosa, cálida y juguetona. Se asombra junto al público y explica con comparaciones "
        "del día a día de un niño."
    ),
    escenario=(
        "Un pequeño teatrito de títeres: cortinas de papel rosa coral a los lados, un borde de papel "
        "kraft y un piso de escenario de madera abajo; al inicio y al final, fondo amarillo mostaza."
    ),
    paleta=["#E9B949", "#E8736B", "#3FA796", "#F3E9D2", "#C9A57A", "#1E2A4F", "#4B3B7A"],
    voz_estilo="narradora joven, cálida y curiosa, suave (tono un poco más juvenil)",
    serie="Teatrito de Papel",
    cta=dict(DEFAULT_CTA),
)


@dataclass
class VideoRequest:
    tema: str
    edad: str = ""     # vacío: la del personaje
    idioma: str = ""   # vacío: el idioma principal del personaje
    formato: str = "vertical"
    slug: str = ""
    model: str = DEFAULT_MODEL
    effort: str = "high"
    max_turns: int = 400
    max_budget_usd: float | None = None
    cta: str = ""
    extra: dict = field(default_factory=dict)
    # Copia del personaje al crear el trabajo: editarlo después no cambia un video a medias.
    personaje: dict = field(default_factory=dict)
    # Videos terminados del mismo personaje, para que el agente copie su diseño.
    referencias: list[str] = field(default_factory=list)
    # Ganchos de sus videos anteriores, para que no repita la misma apertura.
    ganchos_previos: list[str] = field(default_factory=list)

    @property
    def character(self) -> Character:
        return Character.from_dict(self.personaje or LIA.to_dict())

    def validate(self) -> None:
        character = self.character
        if self.personaje:
            character.validate()
            self.personaje = character.to_dict()
        self.edad = str(self.edad or character.edad)
        self.idioma = self.idioma or character.idioma
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
        self.cta = " ".join(self.cta.split()) or character.cta_for(self.idioma)
        if len(self.cta) > 80:
            raise ValueError("La frase final (CTA) debe tener 80 caracteres como máximo.")
        self.slug = slugify(self.slug or f"{self.tema}-{self.idioma}")

    @property
    def out_dir(self) -> Path:
        return OUTPUT_DIR / self.slug

    def variables(self) -> dict[str, str]:
        character = self.character
        voz = character.voces.get(self.idioma) or DEFAULT_VOICES["femenina"][self.idioma]
        return {
            "IDIOMA": self.idioma,
            "TEMA": self.tema,
            "EDAD": self.edad,
            "SLUG": self.slug,
            "FORMATO": self.formato,
            "CTA": self.cta or character.cta_for(self.idioma),
            "PERSONAJE": character.nombre,
            "PERSONAJE_ID": character.id or "personaje",
            "NICHO": character.nicho,
            "APARIENCIA": character.apariencia,
            "PERSONALIDAD": character.personalidad,
            "ESCENARIO": character.escenario,
            "PALETA": ", ".join(f"`{c}`" for c in character.paleta),
            "VOZ": voz,
            "LANG_CODE": voz[0],
            "VOZ_ESTILO": character.voz_estilo or "voz cálida y expresiva",
            "SERIE": character.serie or character.nombre,
            "HASHTAG_SERIE": character.hashtag,
            "REFERENCIAS": self._references_text(character),
            "GANCHOS_PREVIOS": "\n".join(f"- {g}" for g in self.ganchos_previos) or "- (ninguno todavía)",
        }

    def _references_text(self, character: Character) -> str:
        # El agente tiende a copiar el último video de output/: si es de otro personaje,
        # acabaría dibujando a ese otro personaje.
        others = ("No copies el personaje ni el escenario de videos de otros personajes de `output/`; "
                  "de ellos solo puedes reutilizar las herramientas técnicas (voz, sincronía, audio, "
                  "render y QA).")
        if self.referencias:
            dirs = ", ".join(f"`output/{slug}/`" for slug in self.referencias)
            return (f"Ya hay videos de {character.nombre}: {dirs}. Antes de dibujar, abre su `stage.html` "
                    f"(y `tools/`, si existe) y reutiliza tal cual el diseño de {character.nombre} y de su "
                    f"escenario: la serie depende de que se vea siempre igual. {others}")
        return (f"Este es el primer video de {character.nombre}: todavía no tiene dibujo. Diséñalo a partir "
                f"de la ficha del §3, en piezas reutilizables (un grupo SVG o una función para el personaje "
                f"y otro para el escenario), porque los siguientes videos lo copiarán. {others}")


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


async def _ask_json_list(prompt: str) -> list:
    """Una respuesta corta de Claude, sin herramientas, que debe ser una lista JSON."""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    options = ClaudeAgentOptions(model=SUGGEST_MODEL, tools=[], max_turns=1, setting_sources=[],
                                 cwd=str(ROOT))
    text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            text += "".join(b.text for b in message.content if isinstance(b, TextBlock))
    match = re.search(r"\[.*\]", text, re.S)
    try:
        items = json.loads(match.group(0)) if match else []
    except ValueError:
        items = []
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


async def suggest_topics(character: Character, idioma: str, edad: str, pista: str,
                         used: list[str], n: int) -> list[dict]:
    """Pide a Claude n ideas de tema del nicho del personaje [{"tema", "gancho"}].
    Quien llama descarta las repetidas."""
    lines = [
        f"Eres el guionista de «{character.serie}»: videos de 60 segundos con recortes de papel en los "
        f"que {character.nombre} ({character.personalidad}) explica a niños de {edad} años temas de "
        f"este nicho: {character.nicho}.",
        SUGGEST_PROMPT[idioma].format(n=n),
        "Cada tema es una pregunta curiosa de 60 caracteres como máximo, que se pueda explicar y "
        "animar en un minuto. Todos dentro del nicho, pero variados entre sí.",
    ]
    if pista:
        lines.append(f"Deben tratar sobre: {pista}")
    if used:
        lines.append("Estos temas ya están hechos. Puedes proponer temas relacionados o parecidos, "
                     "pero nunca uno igual:\n" + "\n".join(f"- {t}" for t in used))
    lines.append('Responde solo con JSON, sin texto alrededor: [{"tema": "...", "gancho": "una frase corta (15 palabras como máximo) '
                 'sobre por qué le engancha a un niño"}]')
    items = await _ask_json_list("\n\n".join(lines))
    return [{"tema": _clean(i["tema"]), "gancho": str(i.get("gancho") or "").strip()}
            for i in items if _clean(i.get("tema"))]


CHARACTER_PROMPT = """Eres el director creativo de un estudio que produce series de videos cortos (60 s) para niños de {edad} años, animados con recortes de papel (gouache, crayón, bordes rasgados, stop-motion). Cada serie tiene un personaje original que presenta todos sus videos desde un escenario fijo, y un nicho concreto: todos los videos del canal tratan de ese nicho.

Propón {n} personajes muy distintos entre sí (especie o tipo, nicho, escenario y colores), para videos en {lengua}.{pista}

Reglas:
- Personajes originales: nada parecido a personajes, marcas o mascotas con copyright.
- Amables y sin miedo, violencia ni burlas.
- Diseño simple que se pueda recortar en papel (formas grandes, pocos detalles), con ojos y una boca visibles que se puedan animar al hablar, y un objeto característico con el que señala.
- Nicho concreto, con temas para al menos 50 videos de un minuto: no «ciencia» en general, sino, por ejemplo, «animales del océano profundo» o «cómo funcionan las cosas de la cocina».
- El escenario es un lugar fijo hecho de papel (un teatrito, un barco, un laboratorio, una casa en un árbol…) que abre y cierra cada video.{evitar}

Responde solo con JSON, sin texto alrededor:
[{{"nombre": "nombre corto y fácil de pronunciar en español y en inglés",
  "nicho": "el nicho, en {lengua} (60 caracteres como máximo)",
  "gancho": "por qué engancha a los niños (15 palabras como máximo, en español)",
  "apariencia": "descripción visual para el animador: forma, colores, ropa, rasgos y objeto característico (40 a 90 palabras, en español)",
  "personalidad": "cómo es y cómo habla (20 a 50 palabras, en español)",
  "escenario": "el escenario fijo, construido en papel (20 a 50 palabras, en español)",
  "paleta": ["#RRGGBB", "5 colores: 3 de base y 2 de acento, con al menos uno oscuro para la franja de subtítulos"],
  "voz": "femenina o masculina",
  "voz_estilo": "cómo suena su voz (20 palabras como máximo, en español)",
  "serie": "nombre de la serie, en {lengua} (30 caracteres como máximo)",
  "cta_es": "frase final en español, 60 caracteres como máximo, del tipo «¡Sígueme para…!»",
  "cta_en": "la misma frase en inglés"}}]"""


def character_from_idea(idea: dict, idioma: str, edad: str) -> Character:
    """Convierte una idea de personaje (de Claude o del modo demo) en un Character válido."""
    gender = "masculina" if "masc" in str(idea.get("voz") or "").lower() else "femenina"
    character = Character(
        nombre=_clean(idea.get("nombre")), nicho=_clean(idea.get("nicho")),
        apariencia=_clean(idea.get("apariencia")), personalidad=_clean(idea.get("personalidad")),
        escenario=_clean(idea.get("escenario")),
        paleta=[str(c) for c in idea.get("paleta") or [] if HEX_RE.match(str(c))][:7],
        idioma=idioma, edad=edad, voces=dict(DEFAULT_VOICES[gender]),
        voz_estilo=_clean(idea.get("voz_estilo"))[:200], serie=_clean(idea.get("serie"))[:40],
        cta={"es": _clean(idea.get("cta_es"))[:80], "en": _clean(idea.get("cta_en"))[:80]},
    )
    character.validate()
    return character


async def suggest_characters(idioma: str, edad: str, pista: str, avoid: list[str], n: int) -> list[dict]:
    """Pide a Claude n ideas de personaje. Devuelve [{"gancho", "personaje": {...}}] ya validados."""
    prompt = CHARACTER_PROMPT.format(
        edad=edad, n=n, lengua=LANGUAGE_NAMES[idioma],
        pista=f"\n\nEl usuario quiere algo sobre: {pista}" if pista else "",
        evitar=("\n- Ya existen estos personajes; no repitas su nombre ni su nicho:\n"
                + "\n".join(f"  - {a}" for a in avoid)) if avoid else "",
    )
    ideas = []
    for item in await _ask_json_list(prompt):
        try:
            character = character_from_idea(item, idioma, edad)
        except (ValueError, TypeError):
            continue  # una idea incompleta del modelo no tumba las demás
        ideas.append({"gancho": _clean(item.get("gancho")), "personaje": character.to_dict()})
    return ideas


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
    return template.format(tema=req.tema, edad=req.edad, slug=req.slug, personaje=req.character.nombre)


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
    vendor = [name for name in VENDOR_FILES if (VENDOR_DIR / name).exists()]
    ok = all(i["ok"] or i["optional"] for i in items)
    return {"ok": ok, "items": items, "fonts": fonts, "vendor": vendor, "vendor_expected": VENDOR_FILES}


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
        env=agent_env(),
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
