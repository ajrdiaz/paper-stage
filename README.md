# Teatrito de Papel

Aplicación web y agente del Claude Agent SDK que producen **videos educativos infantiles de 60–65 s** con estilo de recortes de papel: la niña Lía guía cada video desde un teatrito de títeres. Está pensada para YouTube Shorts, TikTok y Reels.

El agente investiga, escribe el guion y la biblia visual, graba la voz, anima, compone la música, renderiza y hace el QA. Todo menos el propio agente corre **en local y gratis**:

| Pieza | Herramienta |
|---|---|
| Voz | Kokoro-82M (fallback: Piper) |
| Subtítulos y lip-sync | faster-whisper `small` + envolvente de amplitud |
| Animación | `stage.html` determinista (SVG + Canvas, `window.renderAt(t)`) |
| Música y efectos | `OfflineAudioContext` (fallback: numpy) |
| Render | Playwright + Chromium → ffmpeg |

## Instalación

```bash
./setup.sh          # ffmpeg, espeak-ng, rubberband, .venv, Chromium, fuentes y modelos
source .venv/bin/activate
python run_agent.py --check
```

El agente necesita credenciales de Claude: `ANTHROPIC_API_KEY`, o una sesión de Claude Code ya iniciada.

## Aplicación web

```bash
./run.sh                           # imprime la URL, p. ej. http://127.0.0.1:53817/
./run.sh --demo                    # sin Claude y sin costo, para probar la interfaz
```

- **Estudio**: escribe un tema, elige idioma, formato (9:16 o 16:9) y edad, y el video entra a una cola. Puedes poner varios temas a la vez (uno por línea) y ver el prompt exacto antes de lanzarlo. Las opciones que uses se recuerdan.
- **Producción en vivo**: los 11 pasos del pipeline se marcan según los archivos que el agente va escribiendo, y ves en tiempo real qué hace (herramientas, mensajes, costo, turnos y tiempo). Te avisa cuando un video termina, aunque estés en otra pestaña.
- **Cancelar y reanudar**: cancelar cierra el proceso del agente sin perder lo hecho. Un trabajo fallido, cancelado, interrumpido (por ejemplo, porque se reinició el servidor) o detenido por el tope de gasto se reanuda con la misma sesión y el agente continúa desde el primer paso incompleto.
- **Biblioteca**: todos los videos de `output/`, también los hechos desde la terminal, con portada, duración y resultado del QA.
- **Ficha del video**: reproductor, descarga del MP4 y de los subtítulos, y pestañas con Publicación (títulos, descripción y hashtags con botón de copiar), Guion, Investigación, QA, Biblia visual, Archivos e Historial. Con un clic haces la versión en el otro idioma o encolas una de las «ideas siguientes».
- **Vista interactiva**: reproduce `stage.html` con `mix.wav`, con una barra de tiempo marcada por escenas y avance frame a frame. Sirve para revisar la animación antes (o sin) renderizar.
- **Sistema**: estado de las dependencias locales.

| Opción | Por defecto | |
|---|---|---|
| `--host` | `127.0.0.1` | Con otra dirección (p. ej. `0.0.0.0`) se exige un token de acceso; se genera uno si no defines `PAPER_STAGE_TOKEN`. |
| `--port` | aleatorio | Por defecto el sistema elige un puerto libre, así que varias instancias no chocan. |
| `--concurrency` | `1` | Videos que se producen a la vez. El render usa mucha CPU. |
| `--demo` | apagado | Agente simulado que genera un video de ejemplo. |

Los trabajos y sus registros se guardan en `data/` (SQLite), así que la cola sobrevive a reinicios.

## Terminal

```bash
python run_agent.py --tema "Radiación de Hawking" --edad 6-9
python run_agent.py --tema "Hawking radiation" --idioma en --formato horizontal
python run_agent.py --tema "Volcanes" --max-budget-usd 15
python run_agent.py --tema "Volcanes" --dry-run            # solo imprime el prompt final
python run_agent.py --tema "Volcanes" --resume <session_id>
```

| Opción | Valores | Por defecto |
|---|---|---|
| `--tema` | texto, en el idioma del video | (obligatoria) |
| `--edad` | p. ej. `4-6`, `6-9` | `6-9` |
| `--idioma` | `es`, `en` | `es` |
| `--formato` | `vertical` (1080×1920), `horizontal` (1920×1080) | `vertical` |
| `--slug` | carpeta de salida | tema + idioma |
| `--model` | ID del modelo | `claude-opus-5-5` |
| `--effort` | `low` … `max` | `high` |
| `--max-turns`, `--max-budget-usd` | límites del agente | `400`, sin tope |

## Salida

Cada video queda en `output/<slug>/`: `final.mp4`, `stage.html`, `bible.md`, `script.json`, `research.md`, `words.json`, `subtitles.srt`, `publish.json` y `qa.md`. El registro completo de la sesión está en `agent_log.jsonl`.

## Estructura

| Ruta | Qué es |
|---|---|
| `prompts/system_prompt.md` | Prompt del agente, con `{{IDIOMA}}`, `{{TEMA}}`, `{{EDAD}}`, `{{SLUG}}` y `{{FORMATO}}`. Edítalo para cambiar estilo, escenas o QA. |
| `paper_stage/core.py` | Relleno del prompt y ejecución del agente (compartido por la CLI y la web). |
| `paper_stage/jobs.py` | Cola de trabajos: SQLite, cancelar, reanudar y eventos en vivo. |
| `paper_stage/library.py` | Lectura de `output/`: pasos, QA, duración y portada. |
| `paper_stage/web.py`, `paper_stage/static/` | Servidor (Starlette) e interfaz (HTML, CSS y JS sin compilación). |
| `paper_stage/demo.py` | Agente simulado para `--demo` y para las pruebas. |
| `run_agent.py` | CLI. |

Pruebas: `python -m unittest discover tests`

> **Permisos:** el agente corre con `permission_mode="acceptEdits"` y tiene permitidos `Bash`, `Write`, `Edit`, `WebSearch` y `WebFetch` sin confirmación, porque el pipeline es desatendido. Ejecútalo en una máquina o contenedor dedicado, y no expongas la app a internet.
