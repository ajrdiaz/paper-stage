"""Lógica compartida por la CLI (run_agent.py) y la aplicación web.

Define las series (Serie) y las peticiones de video (VideoRequest), carga
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
import sqlite3
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
VENDOR_FILES = ["gsap.min.js", "MorphSVGPlugin.min.js", "CustomEase.min.js", "three.min.js"]

DEFAULT_MODEL = "claude-opus-5-5"
MODELS = ["claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5", "claude-sonnet-5"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
LANGUAGES = ["es", "en"]
LANGUAGE_NAMES = {"es": "español", "en": "inglés"}
# Frase final por defecto si la serie no tiene una; se puede cambiar por video.
DEFAULT_CTA = {"es": "Sígueme para entender un poco más cada día.", "en": "Follow me to understand a little more every day."}
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
VOICE_PYTHON = ROOT / ".venv-voz" / "bin" / "python"  # Chatterbox va en un entorno aparte: exige otras versiones


def chatterbox_ready() -> bool:
    return VOICE_PYTHON.exists()


ELEVEN_VOICE_RE = re.compile(r"^[A-Za-z0-9]{8,40}$")


def elevenlabs_ready() -> bool:
    """Hay clave de ElevenLabs (variable o guardada en Sistema). No la lee: solo mira que exista."""
    if os.environ.get("PAPER_STAGE_ELEVENLABS_KEY"):
        return True
    db = DATA_DIR / "paper_stage.db"
    if not db.exists():
        return False
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = 'elevenlabs_key'").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


VOZ_CHATTERBOX = """**Voz de la serie: Chatterbox**, con el script de la app, que ya trae la voz y los ajustes de la serie. Escribe `output/{{SLUG}}/audio/lista_voz.json`: `[{"texto": "…", "salida": "output/{{SLUG}}/audio/raw_01.wav"}, …]`, una entrada por escena con todo su texto (320 caracteres como máximo), y ejecútalo una vez, en primer plano y desde la raíz: `.venv-voz/bin/python scripts/voz.py output/{{SLUG}}/audio/lista_voz.json --idioma {{IDIOMA}}`. Tarda unas 12 veces la duración del audio. No uses el `tts.py` de videos anteriores ni otra voz. No hay ajuste de velocidad: si el total queda fuera de la duración, reescribe líneas (con `atempo` puedes ajustar como mucho a 1.08). Solo si el script falla de verdad, usa Kokoro `{{VOZ}}` (lang_code `{{LANG_CODE}}`) y anótalo en `qa.md`."""
# La voz elegida de oído para la serie. Gasta créditos del plan del usuario: se genera una vez por escena.
VOZ_ELEVENLABS = """**Voz de la serie: ElevenLabs**, la voz `{{VOZ_ELEVENLABS}}` elegida de oído por el usuario, con el script de la app, que ya trae la clave y los ajustes. Escribe `output/{{SLUG}}/audio/lista_voz.json`: `[{"texto": "…", "salida": "output/{{SLUG}}/audio/raw_01.wav"}, …]`, una entrada por escena con todo su texto, y ejecútalo una vez, en primer plano y desde la raíz: `.venv/bin/python scripts/voz_elevenlabs.py output/{{SLUG}}/audio/lista_voz.json --voz {{VOZ_ELEVENLABS}}`. Tarda unos segundos por escena, pero **cada generación gasta créditos del plan del usuario** (1 por carácter): nunca regeneres para probar; en las rondas del §6 pasa una lista solo con las escenas que fallaron. No escribas etiquetas entre corchetes ni indicaciones de tono en el texto: se leerían. Junto a cada WAV deja `raw_NN.words.json`, con el inicio y el fin de cada palabra en ese WAV: úsalos para `words.json` (§5, paso 6) sumando el momento en que empieza cada escena en `timeline.json`, en vez de transcribir. Si recortas el silencio inicial de un WAV, resta lo recortado; si le aplicas `atempo`, divide los tiempos por el factor; si haces algo más, vuelve a faster-whisper. No hay ajuste de velocidad: si el total queda fuera de la duración, reescribe líneas (con `atempo` puedes ajustar como mucho a 1.08). No uses otra voz ni el `tts.py` de videos anteriores. Solo si el script falla de verdad (sin créditos, sin conexión o la clave rechazada), {{VOZ_RESPALDO}} y anótalo en `qa.md`."""
VOZ_RESPALDO_CHATTERBOX = ("genera la misma lista con Chatterbox (`.venv-voz/bin/python scripts/voz.py output/{{SLUG}}/audio/lista_voz.json "
                           "--idioma {{IDIOMA}}`, unas 12 veces la duración del audio) y saca `words.json` con faster-whisper")
VOZ_RESPALDO_KOKORO = "usa Kokoro `{{VOZ}}` (lang_code `{{LANG_CODE}}`) y saca `words.json` con faster-whisper"
VOZ_KOKORO = """**Voz de la serie:** Kokoro `{{VOZ}}` (lang_code `{{LANG_CODE}}`). Si tienes que usar Piper y su voz no coincide (femenina o masculina), elige otra voz de Piper del mismo idioma que sí coincida."""
_REMATE = ("Una frase memorable que vuelve a la imagen del inicio, para que el loop se sienta continuo, y una invitación a enviarlo a "
           'una persona concreta que lo vive ("mándaselo a quien…"). Cierra con el CTA exacto, sin cambiarle una palabra: "{{CTA}}"')
_GANCHO = ("Una frase de 3 s de voz como máximo, de uno de los tipos de abajo. La voz empieza en ≤0.3 s, hay movimiento desde el frame 1 y el "
           "gancho va también **escrito** en pantalla (`texto_gancho`, §7), porque muchos ven TikTok sin sonido. El espectador debe "
           "reconocerse: algo que le ha pasado a él o a alguien cercano.")
# Fórmulas fijas como "lo más raro viene ahora" se repetían en todos los videos: cada tema promete lo suyo.
_BUCLE = ("una promesa propia de este tema, que nombra la pieza que falta de la explicación sin revelarla. "
          "Nada de fórmulas genéricas como \"lo más raro viene ahora\", ni la misma frase de los videos anteriores.")
_GIRO = "Entra directo con el dato: sin anunciarlo con \"Lo raro:\" o \"Lo más raro:\"."
_MECANISMO = "Lo invisible hecho visible: la escena más larga y la más espectacular. Es lo que hace que el video se vea dos veces."
# Corta: la de por defecto, sin requisito de minutos. Larga: Creator Rewards pide más de un minuto.
DURATIONS = {
    "corta": {
        "etiqueta": "30–45 s", "min": 30, "max": 45, "escenas": 5, "giro": 4, "palabras_es": "90–125", "palabras_en": "95–130",
        "estructura": f"""| # | Tiempo aprox. | Escena | Función |
|---|---|---|---|
| 1 | 0–4 s | **El gancho** | {_GANCHO} |
| 2 | 4–13 s | **Lo que todos creen y lo que pasa** | La explicación popular, su desmentido y la causa real, con una analogía de un objeto cotidiano que se ve en pantalla. **Bucle abierto:** {_BUCLE} |
| 3 | 13–25 s | **El mecanismo por dentro** | {_MECANISMO} |
| 4 | 25–36 s | **El giro** | El dato que sorprende, respaldado en `research.md`, que cierra el bucle abierto con el clímax visual y musical. {_GIRO} |
| 5 | 36–45 s | **El remate** | {_REMATE} |

En tan poco tiempo no cabe nada de relleno: una sola idea, una sola analogía y un solo dato.""",
    },
    "larga": {
        "etiqueta": "61–70 s", "min": 61, "max": 70, "escenas": 7, "giro": 6, "palabras_es": "170–195", "palabras_en": "180–205",
        "estructura": f"""| # | Tiempo aprox. | Escena | Función |
|---|---|---|---|
| 1 | 0–5 s | **El gancho** | {_GANCHO} |
| 2 | 5–14 s | **Lo que todos creen** | La explicación popular, y su desmentido. **Bucle abierto:** {_BUCLE} |
| 3 | 14–26 s | **Qué pasa de verdad** | La causa real, con una analogía de un objeto cotidiano que se ve en pantalla. |
| 4 | 26–40 s | **El mecanismo por dentro** | {_MECANISMO} |
| 5 | 40–50 s | **El dato que sorprende** | Una cifra o una comparación concreta, respaldada en `research.md`. Micro-gancho propio del tema, no una fórmula. |
| 6 | 50–60 s | **El giro** | Cierra el bucle abierto de la escena 2 con el clímax visual y musical. {_GIRO} Por qué esto le importa al espectador. |
| 7 | 60–70 s | **El remate** | {_REMATE} |""",
    },
}
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
SECRET_ENV = ["PAPER_STAGE_TOKEN", "PAPER_STAGE_BUFFER_KEY", "PAPER_STAGE_APIFY_TOKEN", "PAPER_STAGE_XAI_KEY",
              "PAPER_STAGE_ELEVENLABS_KEY"]


def agent_env() -> dict[str, str]:
    """AGENT_ENV más el entorno virtual de la app al frente del PATH: el agente llama a
    `python3` y `playwright` a secas, y sin esto usaría los del sistema (sin Kokoro,
    Whisper ni Playwright) si la app no se lanzó con el venv activado (p. ej. run.sh)."""
    env = dict(AGENT_ENV)
    # El agente hereda el entorno de la app: que no vea las claves de la app.
    env.update({name: "" for name in SECRET_ENV if os.environ.get(name)})
    if sys.prefix != sys.base_prefix:
        bin_dir = str(Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin"))
        env["VIRTUAL_ENV"] = sys.prefix
        env["PATH"] = os.pathsep.join([bin_dir, os.environ.get("PATH", "")])
    return env
# Sugerir temas es una respuesta corta sin herramientas: basta un modelo rápido.
SUGGEST_MODEL = "claude-sonnet-5"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

KICKOFF = {
    "es": (
        "Produce el video completo de la serie «{serie}» sobre «{tema}», "
        "siguiendo el pipeline de principio a fin en output/{slug}/. "
        "No te detengas hasta tener final.mp4 y los dos QA hechos."
    ),
    "en": (
        "Produce the full video of the series “{serie}” about “{tema}”, "
        "following the pipeline end to end in output/{slug}/. "
        "Do not stop until final.mp4 exists and both QA passes are done. "
        "(The system prompt is in Spanish; everything you produce must be in English.)"
    ),
}

CORRECT = {
    "es": (
        "Este video ya existe en output/{slug}/. Corrige SOLO esto, sin rehacer lo demás:\n{correcciones}\n\n"
        "Reutiliza los scripts de tools/. Antes de sobrescribir final.mp4, mix.wav o music.wav, guarda una copia en "
        "backup/. Si solo cambia el audio, remultiplexa sin recodificar el video. Si cambia stage.html, vuelve a "
        "renderizar solo si hace falta. Si cambia lo que se ve, actualiza `visual` en script.json: la revisión lo compara con el video. Comprueba cada corrección en fotogramas o midiendo, anótala en qa.md y deja final.mp4 listo."
    ),
    "en": (
        "This video already exists in output/{slug}/. Fix ONLY this, without redoing anything else:\n{correcciones}\n\n"
        "Reuse the scripts in tools/. Before overwriting final.mp4, mix.wav or music.wav, keep a copy in backup/. "
        "If only the audio changes, remux without re-encoding the video. Check each fix on frames or by measuring, "
        "note it in qa.md and leave final.mp4 ready. (The system prompt is in Spanish; everything you produce must be in English.)"
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
class Serie:
    """Una serie de videos: su nicho, cómo se ve, cómo suena y su frase final. El presentador
    es opcional: lo normal es que explique una voz sobre la animación."""

    nombre: str
    nicho: str
    estilo: str
    tono: str
    paleta: list[str]
    presentador: str = ""
    idioma: str = "es"  # idioma principal: el de sus videos por defecto
    voces: dict = field(default_factory=lambda: dict(DEFAULT_VOICES["femenina"]))
    voz_estilo: str = ""
    cta: dict = field(default_factory=dict)  # frase final por idioma
    voz_elevenlabs: str = ""  # ID de la voz de ElevenLabs para sus videos en español; vacío: Chatterbox
    id: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Serie":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        for name in ("nombre", "nicho", "estilo", "tono", "presentador", "voz_estilo"):
            setattr(self, name, _clean(getattr(self, name)))
        _check_len("El nombre", self.nombre, 2, 40)
        _check_len("El nicho", self.nicho, 3, 120)
        _check_len("El estilo visual", self.estilo, 20, 800)
        _check_len("El tono", self.tono, 10, 400)
        _check_len("El presentador", self.presentador, 0, 800)
        _check_len("El estilo de voz", self.voz_estilo, 0, 200)
        if self.idioma not in LANGUAGES:
            raise ValueError(f"Idioma no válido: {self.idioma}")
        if not isinstance(self.paleta, list) or not 3 <= len(self.paleta) <= 7 \
                or not all(isinstance(c, str) and HEX_RE.match(c) for c in self.paleta):
            raise ValueError("La paleta debe tener entre 3 y 7 colores en formato #RRGGBB.")
        self.paleta = [c.upper() for c in self.paleta]
        voces = self.voces if isinstance(self.voces, dict) else {}
        for lang in LANGUAGES:
            if voces.get(lang) not in VOICES[lang]:
                raise ValueError(f"Voz no válida para {LANGUAGE_NAMES[lang]}: {voces.get(lang)}")
        self.voces = {lang: voces[lang] for lang in LANGUAGES}
        self.voz_elevenlabs = str(self.voz_elevenlabs or "").strip()
        if self.voz_elevenlabs and not ELEVEN_VOICE_RE.match(self.voz_elevenlabs):
            raise ValueError("Voz de ElevenLabs no válida.")
        cta = self.cta if isinstance(self.cta, dict) else {}
        self.cta = {lang: _clean(cta.get(lang)) for lang in LANGUAGES if _clean(cta.get(lang))}
        if any(len(c) > 80 for c in self.cta.values()):
            raise ValueError("La frase final (CTA) debe tener 80 caracteres como máximo.")

    def cta_for(self, idioma: str) -> str:
        return self.cta.get(idioma) or DEFAULT_CTA[idioma]

    @property
    def hashtag(self) -> str:
        return "#" + slugify(self.nombre).replace("-", "")


# La serie de partida: el estudio siempre tiene al menos una con la que producir. El nombre vale
# para cualquier tema futuro; el nicho sale de lo que mejor funcionó en el Radar.
DEFAULT_SERIE = Serie(
    id="ahora-lo-entiendes",
    nombre="Ahora lo entiendes",
    nicho="Por qué tu cuerpo hace lo que hace: curiosidades del día a día",
    estilo=(
        "3D real renderizado con Three.js a pantalla completa: un cuerpo humano translúcido con efecto fresnel sobre fondo azul noche "
        "casi negro, con vasos, nervios y tejidos luminosos de formas orgánicas, piel con volumen, niebla, partículas y luz. La cámara vuela "
        "sin cortes: entra por la piel, recorre el interior y sale al objeto siguiente. Las analogías son objetos 3D realistas y reconocibles. "
        "Texto grande y muy legible en HTML encima del 3D."
    ),
    tono=(
        "Directo y cercano, como un amigo que sabe mucho. Frases cortas, humor ligero y analogías con objetos de casa. Público principal: México: "
        "usa ejemplos que allí se reconocen al instante (tacos, tortillas, el tianguis, el camión, tu mamá), sin caricaturas ni modismos forzados. "
        "Sorprende sin alarmar y nunca da consejos médicos."
    ),
    paleta=["#0B1026", "#1B2A5B", "#2FD6C3", "#FF8A3D", "#F5F7FF"],
    voz_estilo="narradora adulta, cercana y segura, con ritmo ágil",
    cta=dict(DEFAULT_CTA),
)


@dataclass
class VideoRequest:
    tema: str
    idioma: str = ""   # vacío: el idioma principal de la serie
    formato: str = "vertical"
    duracion: str = "corta"
    slug: str = ""
    model: str = DEFAULT_MODEL
    effort: str = "high"
    max_turns: int = 400
    max_budget_usd: float | None = None
    cta: str = ""
    notas: str = ""    # indicaciones que el usuario ve y aprueba en el Estudio
    inspiracion: list[str] = field(default_factory=list)  # videos del Radar: para avisar si el guion se les parece
    correcciones: list[str] = field(default_factory=list)  # si hay, el trabajo corrige un video ya hecho
    extra: dict = field(default_factory=dict)
    # Copia de la serie al crear el trabajo: editarla después no cambia un video a medias.
    serie: dict = field(default_factory=dict)
    # Videos terminados de la misma serie, para que el agente copie su diseño.
    referencias: list[str] = field(default_factory=list)
    # Ganchos de sus videos anteriores, para que no repita la misma apertura.
    ganchos_previos: list[str] = field(default_factory=list)

    @property
    def ficha(self) -> Serie:
        return Serie.from_dict(self.serie or DEFAULT_SERIE.to_dict())

    def validate(self) -> None:
        ficha = self.ficha
        if self.serie:
            ficha.validate()
            self.serie = ficha.to_dict()
        self.idioma = self.idioma or ficha.idioma
        self.tema = " ".join(self.tema.split())
        if not 2 <= len(self.tema) <= 200:
            raise ValueError("El tema debe tener entre 2 y 200 caracteres.")
        if self.idioma not in LANGUAGES:
            raise ValueError(f"Idioma no válido: {self.idioma}")
        if self.formato not in FORMATS:
            raise ValueError(f"Formato no válido: {self.formato}")
        if self.duracion not in DURATIONS:
            raise ValueError(f"Duración no válida: {self.duracion}")
        if self.effort not in EFFORTS:
            raise ValueError(f"Esfuerzo no válido: {self.effort}")
        if not re.match(r"^[a-z0-9][a-z0-9.-]{1,63}$", self.model):
            raise ValueError(f"Modelo no válido: {self.model}")
        if not 10 <= int(self.max_turns) <= 2000:
            raise ValueError("max_turns debe estar entre 10 y 2000.")
        if self.max_budget_usd is not None and not 0.5 <= float(self.max_budget_usd) <= 1000:
            raise ValueError("El tope de gasto debe estar entre 0.5 y 1000 USD.")
        self.cta = " ".join(self.cta.split()) or ficha.cta_for(self.idioma)
        if len(self.cta) > 80:
            raise ValueError("La frase final (CTA) debe tener 80 caracteres como máximo.")
        self.notas = " ".join(self.notas.split())
        if len(self.notas) > 1500:
            raise ValueError("Las indicaciones deben tener 1500 caracteres como máximo.")
        self.inspiracion = [str(i) for i in self.inspiracion if str(i).isdigit()][:6]
        self.correcciones = [_clean(c)[:800] for c in self.correcciones if _clean(c)][:20]
        self.slug = slugify(self.slug or f"{self.tema}-{self.idioma}")

    @property
    def out_dir(self) -> Path:
        return OUTPUT_DIR / self.slug

    def variables(self) -> dict[str, str]:
        ficha = self.ficha
        voz = ficha.voces.get(self.idioma) or DEFAULT_VOICES["femenina"][self.idioma]
        length = DURATIONS[self.duracion]
        return {
            "ESTRUCTURA": length["estructura"],  # primero: trae dentro otras variables
            "VOZ_MOTOR": self._voice_engine(ficha),
            "VOZ_RESPALDO": VOZ_RESPALDO_CHATTERBOX if chatterbox_ready() else VOZ_RESPALDO_KOKORO,
            "VOZ_ELEVENLABS": ficha.voz_elevenlabs or "(ninguna)",
            "DURACION": self.duracion,
            "DUR_MIN": str(length["min"]),
            "DUR_MAX": str(length["max"]),
            "N_ESCENAS": str(length["escenas"]),
            "ESCENA_GIRO": str(length["giro"]),
            "PALABRAS_ES": length["palabras_es"],
            "PALABRAS_EN": length["palabras_en"],
            "IDIOMA": self.idioma,
            "TEMA": self.tema,
            "SLUG": self.slug,
            "FORMATO": self.formato,
            "CTA": self.cta or ficha.cta_for(self.idioma),
            "NOTAS": self.notas or "(ninguna)",
            "SERIE": ficha.nombre,
            "SERIE_ID": ficha.id or "serie",
            "NICHO": ficha.nicho,
            "ESTILO": ficha.estilo,
            "TONO": ficha.tono,
            "PRESENTADOR": ficha.presentador or "Sin presentador: explica una voz en off sobre la animación.",
            "PALETA": ", ".join(f"`{c}`" for c in ficha.paleta),
            "VOZ": voz,
            "LANG_CODE": voz[0],
            "VOZ_ESTILO": ficha.voz_estilo or "voz cercana y segura",
            "HASHTAG_SERIE": ficha.hashtag,
            "REFERENCIAS": self._references_text(ficha),
            "GANCHOS_PREVIOS": "\n".join(f"- {g}" for g in self.ganchos_previos) or "- (ninguno todavía)",
        }

    def _voice_engine(self, ficha: Serie) -> str:
        # ElevenLabs solo en español, con voz elegida y clave guardada; si no, la local de siempre.
        if self.idioma == "es" and ficha.voz_elevenlabs and elevenlabs_ready():
            return VOZ_ELEVENLABS
        return VOZ_CHATTERBOX if chatterbox_ready() and self.idioma == "es" else VOZ_KOKORO

    def _references_text(self, ficha: Serie) -> str:
        if self.referencias:
            dirs = ", ".join(f"`output/{slug}/`" for slug in self.referencias)
            return (f"Ya hay videos de «{ficha.nombre}»: {dirs}. Antes de dibujar, abre su `stage.html` (y `tools/`, si "
                    "existe) y reutiliza su paleta, materiales, cámara, subtítulos y música: la serie depende de que se reconozca. **No copies sus figuras humanas ni "
                    "sus manos si están armadas con cápsulas o esferas:** ahí manda el §7, que obliga a usar `assets/modelos/cuerpo-torso.js`.")
        return (f"Este es el primer video de «{ficha.nombre}»: todavía no tiene diseño. Créalo a partir de la ficha del "
                "§3, en piezas reutilizables, porque los siguientes videos lo copiarán.")


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


# Palabras que no dicen de qué trata un tema: «¿Por qué te…?» y «Why do you…?» las llevan todos.
_TOPIC_STOP = set("""a al algo ante asi con como cual cuando de del desde donde el ella ellos en entre es esa ese eso esta
este esto estas estos hay la las le les lo los mas me mi mis muy no o otra otro otras otros para pero por porque que se si
sin sobre su sus te tu tus un una uno unos unas y ya justo siempre nunca hace hacen pasa tan tanto da dan pone pones
ponen tiene tienes tienen puede puedes pueden despues luego ver ves cada""".split()) | set(
    """a an and are at be by can do does for from how if in is it its of on or so the to what when where why with you your
    yourself""".split())
SAME_TOPIC = 0.8  # parte de las palabras con contenido del tema más corto que deben estar en el otro


def topic_words(tema: str) -> set[str]:
    """Las raíces de las palabras con contenido de un tema, sin tildes ni plurales: «lucecitas» y «lucecita», o
    «lloras» y «llorar», son la misma (las cinco primeras letras bastan en temas tan cortos)."""
    text = unicodedata.normalize("NFKD", tema).encode("ascii", "ignore").decode().lower()
    words = set()
    for word in re.findall(r"[a-z0-9]+", text):
        if word in _TOPIC_STOP or (len(word) < 2 and not word.isdigit()):  # una cifra sí distingue un tema
            continue
        if len(word) > 4 and word.endswith("es"):
            word = word[:-2]
        elif len(word) > 3 and word.endswith("s"):
            word = word[:-1]
        words.add(word[:5])
    return words


def same_topic(a: str, b: str) -> bool:
    """Dos temas son el mismo aunque cambie la redacción si comparten casi todas sus palabras con contenido:
    «¿Por qué cuando cierras los ojos fuerte ves lucecitas?» y «Por qué ves lucecitas al cerrar fuerte los ojos»."""
    wa, wb = topic_words(a), topic_words(b)
    if not wa or not wb:
        return " ".join(a.lower().split()) == " ".join(b.lower().split())
    return len(wa & wb) / min(len(wa), len(wb)) >= SAME_TOPIC


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


async def suggest_topics(serie: Serie, idioma: str, pista: str, used: list[str], n: int,
                         radar: dict | None = None) -> list[dict]:
    """Pide a Claude n ideas de tema del nicho de la serie [{"tema", "gancho", "radar"}]: primero las del Radar
    que encajen en el nicho (con su id) y luego propias, guiadas por lo que el Radar midió. Quien llama descarta
    las repetidas y comprueba que cada id del Radar exista."""
    lines = [
        f"Eres el guionista de «{serie.nombre}»: videos virales cortos para público general adulto "
        f"sobre este nicho: {serie.nicho}. Tono: {serie.tono}",
        SUGGEST_PROMPT[idioma].format(n=n),
        "Cada tema es una pregunta curiosa de 60 caracteres como máximo, sobre algo que casi todo el mundo "
        "ha vivido, que se pueda explicar y animar en menos de un minuto. Todos dentro del nicho, pero variados entre sí. "
        "Sin diagnósticos, tratamientos ni temas que asusten.",
    ]
    if pista:
        lines.append(f"Deben tratar sobre: {pista}")
    radar = radar or {}
    if radar.get("ideas"):
        lines.append("Ideas del Radar, sacadas de los videos con más vistas de las cuentas que vigila el usuario "
                     f"(nicho del Radar: {radar.get('nicho') or 'sin nombre'}). Ponlas primero, con su tema sin cambiarle "
                     "una palabra y su id en «radar», pero solo las que encajen en el nicho de la serie y no repitan un tema "
                     "ya hecho; las demás, ignóralas:\n" + "\n".join(f"- [{i['id']}] {i['tema']}" for i in radar["ideas"]))
    pattern = radar.get("patrones") or {}
    hints = [f"{label}: " + ", ".join(f"{p['valor']} ({p['veces']})" for p in pattern.get(key) or [])
             for key, label in (("ganchos", "tipos de gancho"), ("temas", "temas")) if pattern.get(key)]
    if hints:
        lines.append("Lo que más funciona en los videos virales del Radar (contado por la app; úsalo de guía para las "
                     "ideas propias, sin copiar temas): " + "; ".join(hints) + ".")
    if used:
        lines.append("Estos temas ya están hechos. Puedes proponer temas relacionados o parecidos, "
                     "pero nunca uno igual:\n" + "\n".join(f"- {t}" for t in used))
    lines.append("Nunca dos temas que sean el mismo con otras palabras, ni entre sí ni con uno ya hecho.")
    lines.append('Responde solo con JSON, sin texto alrededor: [{"tema": "...", "gancho": "una frase corta (15 palabras como máximo) '
                 'sobre por qué engancha", "radar": "el id si es una idea del Radar; si no, vacío"}]')
    items = await _ask_json_list("\n\n".join(lines))
    return [{"tema": _clean(i["tema"]), "gancho": str(i.get("gancho") or "").strip(), "radar": str(i.get("radar") or "").strip()}
            for i in items if isinstance(i, dict) and _clean(i.get("tema"))]


def render_prompt(variables: dict[str, str]) -> str:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    for key, value in variables.items():
        prompt = prompt.replace("{{" + key + "}}", value)
    leftover = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", prompt)))
    if leftover:
        raise ValueError(f"Variables sin reemplazar en el prompt: {', '.join(leftover)}")
    return prompt


def kickoff_prompt(req: VideoRequest, resume: bool = False) -> str:
    if req.correcciones:
        return CORRECT[req.idioma].format(slug=req.slug, correcciones="\n".join(f"- {c}" for c in req.correcciones))
    template = (RESUME if resume else KICKOFF)[req.idioma]
    return template.format(tema=req.tema, slug=req.slug, serie=req.ficha.nombre)


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


def _chromium_installed() -> bool:
    """El módulo playwright no trae el navegador: sin él, el render falla al final del pipeline."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:  # noqa: BLE001 — sin playwright o sin su driver, no hay navegador
        return False


def check_env() -> dict:
    items = []
    for name, (why, optional) in BINARIES.items():
        items.append({"kind": "bin", "name": name, "why": why, "optional": optional,
                      "ok": shutil.which(name) is not None})
    items.append({"kind": "bin", "name": "chatterbox", "why": "voz principal en español (./setup.sh; sin ella, Kokoro)",
                  "optional": True, "ok": chatterbox_ready()})
    items.append({"kind": "bin", "name": "chromium", "why": "render de stage.html (playwright install chromium)",
                  "optional": False, "ok": _chromium_installed()})
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
