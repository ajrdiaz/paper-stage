# Teatrito de Papel

Agente del Claude Agent SDK que produce **videos educativos infantiles de 60–65 s** con estilo de recortes de papel: la niña Lía guía cada video desde un teatrito de títeres. Está pensado para YouTube Shorts, TikTok y Reels.

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

## Uso

```bash
python run_agent.py --tema "Radiación de Hawking" --edad 6-9
python run_agent.py --tema "Hawking radiation" --idioma en
python run_agent.py --tema "Volcanes" --formato horizontal --max-budget-usd 15
python run_agent.py --tema "Volcanes" --dry-run      # solo imprime el prompt final
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

La salida queda en `output/<slug>/`: `final.mp4`, `stage.html`, `bible.md`, `script.json`, `research.md`, `words.json`, `subtitles.srt`, `publish.json` y `qa.md`. El registro completo de la sesión está en `agent_log.jsonl`.

## Cómo funciona

`prompts/system_prompt.md` es el prompt del agente, con las variables `{{IDIOMA}}`, `{{TEMA}}`, `{{EDAD}}`, `{{SLUG}}` y `{{FORMATO}}`. `run_agent.py` las reemplaza y lo pasa como `system_prompt`. Si una variable queda sin reemplazar, el script se detiene. Para cambiar el estilo, la estructura de escenas o el QA, edita ese archivo.

El prompt sigue el método del video de referencia ("Claude Opus 5.5 Is Insane for Educational Animations"). Tiene cinco secciones (Estilo · Personaje · Transiciones · Escenas · Sonido y técnica) y hace la animación como una página HTML autocontenida. Lo adapta a 60–65 s, con estructura viral en 7 escenas y voz local.

> **Permisos:** el agente corre con `permission_mode="acceptEdits"` y tiene permitidos `Bash`, `Write`, `Edit`, `WebSearch` y `WebFetch` sin confirmación, porque el pipeline es desatendido. Ejecútalo en una máquina o contenedor dedicado.
