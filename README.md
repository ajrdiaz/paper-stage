# Radar Studio

Aplicación web y agente del Claude Agent SDK que encuentran lo que funciona en TikTok y producen **videos explicativos virales de 30–45 s o de 61–70 s** para público general adulto. Está pensada para TikTok, YouTube Shorts y Reels. La primera serie es «Ahora lo entiendes».

El agente investiga, escribe el guion y la biblia visual, graba la voz, anima, compone la música, renderiza y hace el QA. Todo menos el propio agente y las lecturas de Apify corre **en local**:

| Pieza | Herramienta |
|---|---|
| Voz | Chatterbox (MIT; añade una marca de agua inaudible), en un entorno aparte; fallback: Kokoro-82M |
| Subtítulos | faster-whisper `small` |
| Animación | `stage.html` determinista (SVG + Canvas o WebGL, `window.renderAt(t)`), con GSAP y Three.js en local |
| Música y efectos | `OfflineAudioContext` (fallback: numpy) |
| Render | Playwright + Chromium → ffmpeg |

## Instalación

```bash
./setup.sh          # ffmpeg, espeak-ng, rubberband, .venv, Chromium, fuentes y modelos
source .venv/bin/activate
python run_agent.py --check
```

El agente necesita credenciales de Claude: una sesión de Claude Code ya iniciada, que consume de tu suscripción (no se cobra aparte), o `ANTHROPIC_API_KEY`, que se paga por uso. Las cifras en dólares de cada trabajo son lo que costaría a precio de API; con suscripción, el límite real son los de tu plan.

## Aplicación web

```bash
./run.sh                           # imprime la URL, p. ej. http://127.0.0.1:53817/
./run.sh --demo                    # sin Claude y sin costo, para probar la interfaz
```

- **Radar**: vigila cuentas de TikTok y muestra qué les funciona en los últimos 14 días. Pega el enlace de un perfil y pulsa «Actualizar». Cada video muestra sus vistas, cuántas veces supera la mediana de su cuenta y sus vistas por día, y es «Viral» con 10× o más y 250K vistas o más; dos lecturas separadas añaden el ritmo actual. «Analizar» transcribe el video y revisa fotogramas con Claude para explicar su gancho, estructura y estilo; sin video descargado, el análisis queda marcado como parcial. «Patrones» resume publicaciones por día, duración, temas y ganchos, y «Nichos» busca hasta 10 nichos con pruebas enlazadas (sin un video o una cuenta de TikTok como prueba, el puntaje no pasa de 6), separados por mercado: México, español en general, Brasil o inglés; con Apify, cada nicho puede buscar sus cuentas reales en TikTok. Al elegir un nicho, «Ideas» propone 5 videos originales que toman el patrón de los virales, con enlace a los que las inspiraron; si el nicho es de otro mercado, las adapta a tu público en lugar de traducirlas. Los datos salen de Apify si guardas su clave en Sistema (cifras exactas; en el plan gratis, de $5 al mes y sin tarjeta, leer 150 videos costó $0,56 y analizar uno, menos de 1 centavo) o, si no, de una ventana con un perfil de navegador propio donde inicias sesión o verificas tú, con una cuenta secundaria (solo vistas redondeadas, sin duración, me gusta ni guardados). El Radar no actualiza solo: cada lectura la pides tú.
- **Series**: cada serie tiene su nombre, **nicho concreto** (todos sus videos giran alrededor de él), estilo visual, tono, presentador opcional, paleta, voz de Kokoro en cada idioma, frase final y cuenta de TikTok.
- **Estudio**: elige la serie y escribe un tema o llega desde una idea del Radar con «Producir», que rellena el tema y las indicaciones para el agente (revísalas: es lo único del Radar que le llega). También puedes pulsar «Sugerir 3 temas». Elige idioma, formato (9:16 o 16:9), duración (30–45 s por defecto, o 61–70 s, la que pide Creator Rewards) y frase final (por defecto la de la serie, editable y recordada), y el video entra a una cola. Los temas no se repiten dentro de una serie: se rechaza uno igual a otro ya hecho o en cola en el mismo idioma, aunque cambien mayúsculas, tildes o signos; uno parecido sí vale. Puedes poner varios temas a la vez (uno por línea) y ver el prompt exacto antes de lanzarlo. Las opciones que uses se recuerdan.
- **Producción en vivo**: los 12 pasos del pipeline (con un QA previo antes del render y un QA final después) se marcan según los archivos que el agente va escribiendo, y ves en tiempo real qué hace (herramientas, mensajes, costo, turnos y tiempo). Te avisa cuando un video termina, aunque estés en otra pestaña.
- **Cancelar y reanudar**: cancelar cierra el proceso del agente sin perder lo hecho. Un trabajo fallido, cancelado, interrumpido (por ejemplo, porque se reinició el servidor) o detenido por el tope de gasto se reanuda con la misma sesión y el agente continúa desde el primer paso incompleto.
- **Revisión automática**: al terminar cada video, la app mide por su cuenta `final.mp4` (duración, resolución, fps, códecs, volumen), cuándo empieza la voz en `voice.wav` (≤0.3 s), el gancho escrito en pantalla (6 palabras como máximo) y `publish.json` (5 hashtags, descripción corta, títulos) y, si el video salió de una idea del Radar, que el guion no copie a los videos que lo inspiraron, sin fiarse del QA del agente. Si algo no cumple, lo avisa en la producción, en la ficha y en la biblioteca («Revisar»).
- **Publicar en TikTok** (Buffer + Google Drive): configúralo una vez en Sistema. Pasos: una carpeta de Google Drive para escritorio compartida como «Cualquier persona con el enlace», la clave de API de Buffer y tus cuentas de TikTok conectadas en Buffer; «Probar la configuración» lo comprueba con un video de 1 s. Cada serie publica en la cuenta de TikTok que elijas en su ficha (varias pueden compartirla); una sin cuenta no puede publicar. Con más de una cuenta aparece arriba el selector «Cuenta», que muestra solo las series, trabajos y videos de esa cuenta. Después, en la pestaña Publicación de cada video, «Publicar en TikTok» copia `final.mp4` a esa carpeta, espera a que Drive lo suba, comprueba que el enlace sirve el video y crea la publicación en Buffer: ahora o programada. Usa como portada el fotograma de `frame_portada_s` y, si lo marcas, declara en TikTok el contenido generado con IA. Deja el video en Drive hasta que se publique: Buffer lo descarga en ese momento.
- **Biblioteca**: todos los videos de `output/`, también los hechos desde la terminal, con portada, duración y resultado del QA.
- **Revisión de audio y video**: al terminar cada video, la app transcribe la voz y la compara con el guion palabra por palabra, mide ritmo, pausas, picos, congelados, negros y cortes, y Claude revisa un fotograma cada 3 s. Las correcciones salen en la pestaña Revisión, ordenadas por gravedad, con su fotograma y su segundo: marca las que quieras, añade las tuyas y pulsa «Corregir lo seleccionado» para que el agente las aplique. Nadie oye el video: la naturalidad y la entonación debes comprobarlas tú.
- **Ficha del video**: reproductor, descarga del MP4 y de los subtítulos, y pestañas con Revisión, Publicación (títulos, descripción corta para TikTok y Reels, descripción larga para YouTube y hashtags, con botón de copiar, y la portada `portada.jpg` para descargar: el fotograma elegido, sin subtítulos y con el texto de portada), Guion, Investigación, QA, Biblia visual, Archivos e Historial. Con un clic haces la versión en el otro idioma o encolas una de las «ideas siguientes».
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
python run_agent.py --tema "¿Por qué te da hipo?"
python run_agent.py --tema "Why do you get hiccups?" --idioma en --formato horizontal
python run_agent.py --tema "¿Por qué te da hipo?" --max-budget-usd 15
python run_agent.py --tema "¿Por qué te da hipo?" --dry-run   # solo imprime el prompt final
python run_agent.py --tema "¿Por qué te da hipo?" --resume <session_id>
```

| Opción | Valores | Por defecto |
|---|---|---|
| `--tema` | texto, en el idioma del video | (obligatoria) |
| `--idioma` | `es`, `en` | el idioma principal de la serie |
| `--formato` | `vertical` (1080×1920), `horizontal` (1920×1080) | `vertical` |
| `--duracion` | `corta` (30–45 s), `larga` (61–70 s) | `corta` |
| `--serie` | ID de la serie (`--series` las lista; se crean en la app web) | la primera |
| `--cta` | frase final del video | la de la serie |
| `--slug` | carpeta de salida | tema + idioma |
| `--model` | ID del modelo | `claude-opus-5-5` |
| `--effort` | `low` … `max` | `high` |
| `--max-turns`, `--max-budget-usd` | límites del agente | `400`, sin tope |

## Salida

Cada video queda en `output/<slug>/`: `final.mp4`, `stage.html`, `bible.md`, `script.json`, `research.md`, `words.json`, `subtitles.srt`, `publish.json` y `qa.md`. El registro completo de la sesión está en `agent_log.jsonl`.

## Estructura

| Ruta | Qué es |
|---|---|
| `prompts/system_prompt.md` | Prompt del agente, con sus variables `{{X}}` (lista en `AGENTS.md`). Edítalo para cambiar estilo, escenas, reglas de contenido o QA. |
| `paper_stage/core.py` | Series, relleno del prompt y ejecución del agente (compartido por la CLI y la web). |
| `paper_stage/jobs.py` | Cola de trabajos: SQLite, cancelar, reanudar y eventos en vivo. |
| `paper_stage/library.py` | Lectura de `output/`: pasos, QA, revisión automática, duración y portada. |
| `paper_stage/publishing.py` | Publicación en TikTok con Buffer y Google Drive. |
| `paper_stage/radar.py` | Radar: cuentas de TikTok, lecturas, ranking, análisis, nichos e ideas. |
| `paper_stage/review.py` | Revisión de audio y video de cada video terminado, con sus sugerencias. |
| `paper_stage/web.py`, `paper_stage/static/` | Servidor (Starlette) e interfaz (HTML, CSS y JS sin compilación). |
| `paper_stage/demo.py` | Agente simulado para `--demo` y para las pruebas. |
| `run_agent.py` | CLI. |

Pruebas: `python -m unittest discover tests`

> **Permisos:** el agente corre con `permission_mode="acceptEdits"` y tiene permitidos `Bash`, `Write`, `Edit`, `WebSearch` y `WebFetch` sin confirmación, porque el pipeline es desatendido. Ejecútalo en una máquina o contenedor dedicado, y no expongas la app a internet.
