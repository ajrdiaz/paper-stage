# AGENTS.md — Teatrito de Papel

Guía para agentes de código que trabajan en este repositorio. Para el uso de la app, ver `README.md`.

## Qué es

Una app web y una CLI lanzan un agente del Claude Agent SDK que produce videos educativos infantiles de 60–65 s con estilo de recortes de papel. En ellos, la niña Lía guía cada video desde un teatrito. Todo corre en local salvo el propio agente: voz Kokoro, faster-whisper, `stage.html` renderizado con Playwright, y música y efectos sintetizados por código.

El repositorio tiene dos partes que no hay que confundir:

- **La app** (`paper_stage/`, `run_agent.py`, `prompts/`, `tests/`): es el código que se mantiene.
- **Los videos** (`output/<slug>/`): los genera el agente en cada ejecución, y cada uno trae sus propios scripts en `tools/`. No son código compartido.

## Idioma

Todo en español: respuestas al usuario, comentarios, docstrings, mensajes de error, textos de la interfaz y mensajes de commit. Los videos pueden ser `es` o `en`, pero el prompt del sistema está escrito en español.

## Comandos

```bash
./setup.sh                                  # dependencias del sistema, .venv (Python 3.10–3.12), Chromium, fuentes y modelos
.venv/bin/python -m unittest discover tests # pruebas (~30 s; usan el agente demo, sin Claude ni costo)
./run.sh --demo                             # app web con agente simulado
./run.sh                                    # app web real (imprime la URL; puerto aleatorio)
.venv/bin/python run_agent.py --tema "Volcanes" --dry-run   # imprime el prompt final sin lanzar nada
.venv/bin/python run_agent.py --check       # estado de las dependencias locales
```

Usa siempre `.venv/bin/python`, porque el `python3` del sistema no tiene Kokoro, Whisper ni Playwright. No hay paso de compilación: el frontend (`paper_stage/static/`) es HTML, CSS y JS sin herramientas.

## Mapa del código

| Ruta | Responsabilidad |
|---|---|
| `prompts/system_prompt.md` | Prompt del agente: pipeline de 12 pasos, estructura de 7 escenas, reglas de QA y de `publish.json`. Variables: `{{IDIOMA}}`, `{{TEMA}}`, `{{EDAD}}`, `{{SLUG}}`, `{{FORMATO}}`, `{{CTA}}`. |
| `paper_stage/core.py` | `VideoRequest` y su validación, relleno del prompt, `run_agent()` (opciones del SDK, herramientas permitidas y entorno), sugerencia de temas, `slugify`/`topic_key`. |
| `paper_stage/jobs.py` | `JobStore` (SQLite en `data/`) y `JobManager`: cola, concurrencia, cancelar, reanudar y eventos en vivo (SSE). |
| `paper_stage/library.py` | Lectura de `output/`: `STEPS` (paso → archivos que lo marcan como hecho), revisión automática de `final.mp4` (`verify`), portada y póster. |
| `paper_stage/web.py` | Servidor Starlette y API; `TokenAuth` si hay `PAPER_STAGE_TOKEN`. |
| `paper_stage/demo.py` | Agente simulado que escribe un video falso completo (para `--demo` y las pruebas). |
| `run_agent.py` | CLI. |
| `tests/test_app.py` | Pruebas de extremo a extremo con el agente demo y directorios temporales. |

Variables de entorno útiles: `PAPER_STAGE_OUTPUT`, `PAPER_STAGE_DATA` (redirigen `output/` y `data/`; las pruebas las usan), `PAPER_STAGE_DEMO=1`, `PAPER_STAGE_DEMO_DELAY`, `PAPER_STAGE_CONCURRENCY` y `PAPER_STAGE_TOKEN`.

## Invariantes (no romper)

- **Prompt ↔ app.** Si cambias los pasos o los archivos de salida en `system_prompt.md`, actualiza `STEPS` en `library.py`, los textos `KICKOFF`/`RESUME` de `core.py` y `demo.py` (que imita la salida real). Si añades una variable `{{X}}`, rellénala en `core.py`: una prueba verifica que no quede ninguna sin rellenar.
- **Límites de publicación.** `MAX_HASHTAGS`, `MAX_SHORT_DESCRIPTION` y `MAX_TITLE` (`library.py`) deben coincidir con lo que exige §11 del prompt. Lo mismo vale para la duración (60–65 s), las resoluciones, 24 fps, H.264 `yuv420p`, AAC 48 kHz y −14 LUFS ±1.
- **El agente trabaja en primer plano.** `AGENT_ENV` desactiva las tareas en segundo plano y `DISALLOWED_TOOLS` bloquea las de «esperar»: al cerrar el turno, el proceso de Claude Code mata lo que siga corriendo, como un render a medias. `agent_env()` pone el `.venv` al frente del `PATH` del agente; no lo quites.
- **Seguridad.** El agente corre con `permission_mode="acceptEdits"` y con `Bash` sin confirmación. Fuera de `127.0.0.1`, la app exige token. No debilites esto.
- **Modelos.** Los IDs válidos están en `MODELS` (`core.py`); el modelo por defecto es `claude-opus-5-5`.
- **Reanudar sin contar dos veces.** El costo y los turnos de una sesión reanudada no deben sumarse dos veces; hay una prueba para eso.

## Revisar o corregir un video ya hecho (`output/<slug>/`)

- No te fíes de `qa.md` ni de `qa_previo.md`: son informes del propio agente. Mide sobre `final.mp4` con `ffprobe` y `ffmpeg -af ebur128`, extrae fotogramas y transcribe `mix.wav` con faster-whisper `small` sin prompt.
- Los scripts de cada video están en `output/<slug>/tools/`: `tts.py`, `sync.py`, `render_audio.py`, `mix.py`, `render.py`, `qa.py`, etc. Úsalos en lugar de reescribirlos, y ejecútalos con `.venv/bin/python`.
- **Cambios solo de audio** (música en `audio.html`, efectos en `sfx_cues.json` o la mezcla): `tools/render_audio.py` → `tools/mix.py` → remultiplexa sin recodificar el video (`ffmpeg -i final.mp4 -i mix.wav -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 192k -ar 48000 -shortest`) → `tools/qa.py`.
- **Cambios de guion o de voz** obligan a rehacer TTS, `timeline.json`, `words.json`, `mouth.json`, subtítulos y el render. Avisa del costo antes de hacerlo.
- Antes de sobrescribir `final.mp4`, `mix.wav` o `music.wav`, guarda una copia. Anota cada corrección en `qa.md`.
- `verify.json` es la caché de la revisión automática de la app. Se invalida sola cuando cambia la fecha de modificación de sus archivos fuente; no la edites a mano.
- `output/` y `data/` están en `.gitignore`: los videos y la base de datos de trabajos viven solo en disco. No los añadas al repositorio (ni con `git add -f`).

## Estilo de código

- Python 3.10+ con `from __future__ import annotations`, dataclasses y tipos en las firmas. Comentarios breves en español que explican el porqué.
- Imita el código que rodea tu cambio. No añadas dependencias sin actualizar `requirements.txt` y, si son binarios del sistema, `setup.sh`.
- Ejecuta las pruebas antes de dar un cambio por terminado.
