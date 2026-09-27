#!/usr/bin/env bash
# Inicia la aplicación web en un puerto libre aleatorio (imprime la URL).
# Los argumentos se pasan tal cual: ./run.sh --demo, ./run.sh --port 8080, ...
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import claude_agent_sdk, uvicorn" 2>/dev/null; then
  echo "Falta el entorno (.venv) o sus dependencias; ejecuta primero ./setup.sh" >&2
  exit 1
fi

exec .venv/bin/python -m paper_stage "$@"
