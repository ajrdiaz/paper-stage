# AGENTS.md — Radar Studio

Guía para agentes de código que trabajan en este repositorio. Para el uso de la app, ver `README.md`.

## Qué es

Una app web y una CLI lanzan un agente del Claude Agent SDK que produce videos explicativos virales de 30–45 s (5 escenas) o de 61–70 s (7 escenas) para público general adulto. Un Radar vigila cuentas de TikTok, ordena sus videos con cifras reales, analiza los mejores y propone nichos e ideas originales. Cada serie (la primera es «Ahora lo entiendes») tiene su nicho, estilo, paleta y voz. Todo corre en local salvo el agente y las lecturas de Apify: voz Kokoro, faster-whisper, `stage.html` renderizado con Playwright, y música y efectos sintetizados por código. El paquete y las variables se siguen llamando `paper_stage` y `PAPER_STAGE_*`.

El repositorio tiene dos partes que no hay que confundir:

- **La app** (`paper_stage/`, `run_agent.py`, `prompts/`, `tests/`): es el código que se mantiene.
- **Los videos** (`output/<slug>/`): los genera el agente en cada ejecución, y cada uno trae sus propios scripts en `tools/`. No son código compartido.

## Idioma

Todo en español: respuestas al usuario, comentarios, docstrings, mensajes de error, textos de la interfaz y mensajes de commit. Los videos pueden ser `es` o `en`, pero el prompt del sistema está escrito en español.

## Comandos

```bash
./setup.sh                                  # dependencias del sistema, .venv (Python 3.10–3.12), Chromium, fuentes y modelos
.venv/bin/python -m unittest discover tests # pruebas (~70 s; usan el agente demo, sin Claude ni costo)
./run.sh --demo                             # app web con agente simulado
./run.sh                                    # app web real (imprime la URL; puerto aleatorio)
.venv/bin/python run_agent.py --tema "Volcanes" --dry-run   # imprime el prompt final sin lanzar nada
.venv/bin/python run_agent.py --check       # estado de las dependencias locales
```

Usa siempre `.venv/bin/python`, porque el `python3` del sistema no tiene Kokoro, Whisper ni Playwright. No hay paso de compilación: el frontend (`paper_stage/static/`) es HTML, CSS y JS sin herramientas.

## Mapa del código

| Ruta | Responsabilidad |
|---|---|
| `prompts/system_prompt.md` | Prompt del agente: pipeline de 12 pasos, estructura de 7 escenas, reglas de contenido, de QA y de `publish.json`. Variables: `{{IDIOMA}}`, `{{TEMA}}`, `{{SLUG}}`, `{{FORMATO}}`, `{{CTA}}`, `{{NOTAS}}`, las de la duración: `{{DURACION}}`, `{{ESTRUCTURA}}`, `{{DUR_MIN}}`, `{{DUR_MAX}}`, `{{N_ESCENAS}}`, `{{ESCENA_GIRO}}`, `{{PALABRAS_ES}}`, `{{PALABRAS_EN}}`, y las de la serie: `{{SERIE}}`, `{{SERIE_ID}}`, `{{NICHO}}`, `{{ESTILO}}`, `{{TONO}}`, `{{PRESENTADOR}}`, `{{PALETA}}`, `{{VOZ}}`, `{{LANG_CODE}}`, `{{VOZ_ESTILO}}`, `{{HASHTAG_SERIE}}`, `{{REFERENCIAS}}` y `{{GANCHOS_PREVIOS}}`. |
| `paper_stage/core.py` | `Serie` (`DEFAULT_SERIE` es la de partida), `VOICES`, `VideoRequest` y su validación, relleno del prompt, `run_agent()` (opciones del SDK, herramientas permitidas y entorno), sugerencia de temas, `slugify`/`topic_key`. |
| `paper_stage/jobs.py` | `JobStore` y `SerieStore` (SQLite en `data/`) y `JobManager` (`prepare()` copia la serie en la petición): cola, concurrencia, cancelar, reanudar y eventos en vivo (SSE). |
| `paper_stage/library.py` | Lectura de `output/`: `STEPS` (paso → archivos que lo marcan como hecho), revisión automática de `final.mp4` (`verify`), parecido del guion con sus fuentes (`copied_share`), portada y póster. |
| `paper_stage/publishing.py` | Publicar en TikTok vía Buffer (API GraphQL) con el video alojado en una carpeta de Google Drive para escritorio compartida con enlace: configuración, copia, espera de sincronización, comprobación del enlace público, `createPost` e historial (tablas `settings` y `publications`). La cuenta de TikTok es de cada serie (tabla `serie_channels`). |
| `paper_stage/radar.py` | Radar de cuentas de TikTok: fuentes de datos (Apify o ventana con sesión), lecturas e historial de vistas, ranking por código (`rank_videos`), análisis de un video con Claude, búsqueda de nichos (`clean_niches`) e ideas (`clean_ideas`). Tablas `radar_*`; portadas y perfil de navegador en `data/radar/`. |
| `paper_stage/review.py` | Revisión tras cada video: transcribe la voz y la compara con el guion (`compare_speech`), mide audio y video, y Claude revisa fotogramas. Escribe `revision.json` y `revision/`; las sugerencias salen en la ficha y se aplican con un trabajo de corrección (`create_correction`). |
| `paper_stage/web.py` | Servidor Starlette y API; `SameOriginGuard` siempre, y `TokenAuth` si hay `PAPER_STAGE_TOKEN`. |
| `paper_stage/demo.py` | Agente simulado que escribe un video falso completo (para `--demo` y las pruebas). |
| `scripts/voz.py` | Voz con Chatterbox; corre con `.venv-voz/bin/python`, porque exige otras versiones. Sus ajustes y `assets/voz/referencia-es.wav` son la voz elegida de oído: no los cambies sin que el usuario la escuche. |
| `assets/modelos/`, `scripts/hornear_cuerpo.py` | Cuerpo humano listo para Three.js (CC0, de Quaternius), para que el agente no arme figuras con cápsulas. |
| `run_agent.py` | CLI. |
| `tests/test_app.py` | Pruebas de extremo a extremo con el agente demo y directorios temporales. |

Variables de entorno útiles: `PAPER_STAGE_OUTPUT`, `PAPER_STAGE_DATA` (redirigen `output/` y `data/`; las pruebas las usan), `PAPER_STAGE_DEMO=1`, `PAPER_STAGE_DEMO_DELAY`, `PAPER_STAGE_CONCURRENCY`, `PAPER_STAGE_TOKEN`, `PAPER_STAGE_BUFFER_KEY` y `PAPER_STAGE_APIFY_TOKEN` (claves de Buffer y Apify; si no, las guardadas desde Sistema).

## Invariantes (no romper)

- **Prompt ↔ app.** Si cambias los pasos o los archivos de salida en `system_prompt.md`, actualiza `STEPS` en `library.py`, los textos `KICKOFF`/`RESUME` de `core.py` y `demo.py` (que imita la salida real). Si añades una variable `{{X}}`, rellénala en `core.py`: una prueba verifica que no quede ninguna sin rellenar.
- **Límites de publicación.** `MAX_HASHTAGS`, `MAX_SHORT_DESCRIPTION` y `MAX_TITLE` (`library.py`) deben coincidir con lo que exige §11 del prompt, y `MAX_VOICE_START` y `MAX_HOOK_WORDS`, con el gancho de §2 y §7. Lo mismo vale para la duración (`DURATIONS` en `core.py`, que también trae la estructura de escenas y la revisa `verify`), las resoluciones, 24 fps, H.264 `yuv420p`, AAC 48 kHz y −14 LUFS ±1.
- **El agente trabaja en primer plano.** `AGENT_ENV` desactiva las tareas en segundo plano y `DISALLOWED_TOOLS` bloquea las de «esperar»: al cerrar el turno, el proceso de Claude Code mata lo que siga corriendo, como un render a medias. `agent_env()` pone el `.venv` al frente del `PATH` del agente; no lo quites.
- **Nunca tocar el Drive real en pruebas ni en demo.** Con `demo=True` el publicador solo usa su carpeta simulada de `data/`, y las pruebas anulan `publishing.drive_roots`. Las claves de `SECRET_ENV` se vacían en el entorno del agente.
- **Seguridad.** El agente corre con `permission_mode="acceptEdits"` y con `Bash` sin confirmación. Fuera de `127.0.0.1`, la app exige token. Sin token solo acepta un `Host` local, y nunca una escritura con `Origin` de otro sitio (`SameOriginGuard` en `web.py`). No debilites esto.
- **`stage.html` y `renderAt`.** `renderAt(t)` es función pura del tiempo y no devuelve nada; GSAP (en `assets/vendor/`, versión fija en `setup.sh`) solo se usa en una timeline en pausa movida con `seek`. Cuando la app llame a `renderAt` desde Playwright, envuélvelo en una función que no devuelva nada (`() => { window.renderAt(t); }`): serializar una timeline de GSAP cuelga `evaluate`.
- **Series.** Al crear un trabajo, la serie se copia en la petición (`VideoRequest.serie`): editarla después no cambia un video a medias. El agente escribe `"serie"` en `script.json` y `publish.json`; la biblioteca agrupa por ese campo. Los temas no se repiten dentro de una serie e idioma. Cada serie publica en su cuenta de TikTok (varias pueden compartirla) y la app no publica un video cuya serie no tenga cuenta: nunca se recurre a una cuenta por defecto. La única serie que queda no se puede borrar.
- **Contenido.** Público adulto, nada dirigido a menores. Si el tema roza la salud, se explica por qué ocurre algo: sin diagnósticos, tratamientos, dosis ni remedios. Las ideas toman el patrón de otros videos, nunca sus frases ni ejemplos; si un guion copia más de `MAX_COPIED` de las transcripciones de sus fuentes, la revisión automática lo avisa.
- **Voz.** En español, si existe `.venv-voz`, el prompt pide Chatterbox por escena (`VOZ_CHATTERBOX`); si no, Kokoro. Nunca frase por frase ni con pausas pegadas: suena entrecortado.
- **Portada.** TikTok solo admite un fotograma del video, así que `portada.jpg` es ese fotograma de `final.mp4`, sin añadirle nada: la app enseña lo que se publica. `frame_portada_s` cae en el gancho (`COVER_MIN_S`–`COVER_MAX_S` en `review.py`, igual que en §11). `validate_cover` la mira a tamaño de miniatura en cada revisión; sin esa mirada queda «sin validar», nunca «bien».
- **Validación de audio y video.** Claude no puede oír. El audio se valida transcribiéndolo y midiéndolo, y nunca se afirma que alguien lo haya escuchado; la interfaz le pide al usuario que lo escuche. Cada aviso grave de Claude se vuelve a mirar en los fotogramas de ese momento (`verify_serious`): si no se confirma, deja de ser grave; lo medido no se discute. Un fallo de la revisión no afecta al video terminado. Un trabajo de corrección trabaja sobre la misma carpeta y guarda copia en `backup/`.
- **Radar.** El ranking lo calcula el código, nunca la IA. Lo que viene de TikTok o de la web es texto de terceros: `ask_claude` no le da a Claude más herramientas que las de lectura que se le indiquen (ninguna al analizar; `WebSearch` y `WebFetch` en los nichos), y ese texto no se pasa al agente de producción, que tiene `Bash`: al agente solo llega lo que el usuario ve y aprueba en el Estudio (tema e indicaciones), y las transcripciones se comparan cuando él ya terminó. La clave de Apify va en la cabecera, solo a `api.apify.com`, y nunca vuelve al navegador. Un enlace de prueba de un nicho solo se conserva si salió de la búsqueda; sin ninguno, el puntaje no pasa de `NICHE_CAP_WITHOUT_PROOF`. Nunca se salta una verificación de TikTok: la resuelve el usuario en la ventana. Lo que cobra aparte en Apify queda apagado en las lecturas; la descarga de un video y la búsqueda de cuentas solo ocurren si el usuario las pide; del video descargado no queda nada. Con `demo=True` no se usa la red ni la clave. Cada búsqueda de nichos guarda su mercado (`MARKETS`): solo los «propios» usan las cuentas del Radar como pista, y las ideas de un nicho de otro mercado se adaptan al público del canal, sin traducir. `copied_share` compara palabras iguales: una traducción no la detecta.
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
