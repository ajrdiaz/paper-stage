#!/usr/bin/env bash
# Instala todo lo que el pipeline local necesita (Debian/Ubuntu o macOS con Homebrew).
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Binarios del sistema (ffmpeg, espeak-ng, rubberband)"
if command -v apt-get >/dev/null; then
  SUDO=$([ "$(id -u)" -eq 0 ] && echo "" || echo "sudo")
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq ffmpeg espeak-ng rubberband-cli
elif command -v brew >/dev/null; then
  brew install ffmpeg espeak-ng rubberband
else
  echo "Instala ffmpeg, espeak-ng y rubberband con tu gestor de paquetes." >&2
fi

echo "==> Entorno de Python (.venv)"
# claude-agent-sdk y Kokoro piden Python 3.10–3.12 (el python3 de macOS es 3.9).
PY_OK='import sys; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 12)))'
PYTHON=""
for cand in "${PYTHON_BIN:-}" python3.12 python3.11 python3.10 python3; do
  if [ -n "$cand" ] && command -v "$cand" >/dev/null && "$cand" -c "$PY_OK" 2>/dev/null; then
    PYTHON="$cand"; break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "Se necesita Python 3.10–3.12 (p. ej. 'brew install python@3.12'), o indícalo con PYTHON_BIN=/ruta/python." >&2
  exit 1
fi
if [ -x .venv/bin/python ] && ! .venv/bin/python -c "$PY_OK" 2>/dev/null; then
  echo "El .venv usa $(.venv/bin/python --version 2>&1); se recrea con $("$PYTHON" --version 2>&1)."
  rm -rf .venv
fi
"$PYTHON" -m venv .venv
.venv/bin/pip install -q --upgrade pip
# En Linux, PyTorch (dependencia de Kokoro) trae CUDA por defecto (varios GB).
# Usamos la versión CPU salvo que se pida GPU con USE_CUDA=1.
if [ "$(uname -s)" = "Linux" ] && [ "${USE_CUDA:-0}" != "1" ]; then
  .venv/bin/pip install -q torch --index-url https://download.pytorch.org/whl/cpu \
    || echo "Aviso: no se pudo usar el índice CPU de PyTorch; se instalará desde PyPI." >&2
fi
.venv/bin/pip install -q -r requirements.txt

echo "==> Chromium para Playwright"
.venv/bin/playwright install chromium \
  || echo "Aviso: no se pudo instalar Chromium; usa executable_path con un Chromium existente." >&2

echo "==> Fuentes (Google Fonts, licencia OFL) → assets/fonts/"
mkdir -p assets/fonts
base="https://github.com/google/fonts/raw/main/ofl"
for f in "gaegu/Gaegu-Regular.ttf" "gaegu/Gaegu-Bold.ttf" \
         "patrickhand/PatrickHand-Regular.ttf" "fredoka/Fredoka%5Bwdth,wght%5D.ttf"; do
  out="assets/fonts/$(basename "$f" | sed 's/%5B.*%5D/-Variable/')"
  [ -f "$out" ] || curl -fsSL "$base/$f" -o "$out" \
    || { rm -f "$out"; echo "Aviso: no se pudo descargar $f (el agente la buscará)." >&2; }
done

echo "==> Precarga de modelos locales (Kokoro, faster-whisper small)"
.venv/bin/python - <<'PY' || echo "Aviso: no se pudieron precargar los modelos (¿acceso a huggingface.co?). Se descargarán en el primer uso." >&2
from kokoro import KPipeline
for lang, voice in (("e", "ef_dora"), ("a", "af_heart")):
    for _ in KPipeline(lang_code=lang)("Hola.", voice=voice):
        pass
from faster_whisper import WhisperModel
WhisperModel("small", device="cpu", compute_type="int8")
print("Modelos listos.")
PY

.venv/bin/python run_agent.py --check
