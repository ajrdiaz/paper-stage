"""Genera la voz de un video con ElevenLabs. Se ejecuta con el entorno de la app:

    .venv/bin/python scripts/voz_elevenlabs.py output/<slug>/audio/lista_voz.json --voz <id de la voz>

lista_voz.json es la misma lista que usa scripts/voz.py: [{"texto": "...", "salida": "...raw_01.wav"}, ...], una
entrada por escena. Junto a cada WAV deja <nombre>.words.json: sus palabras con el inicio y el fin en ese WAV, según
los tiempos de ElevenLabs. La clave es la guardada en Sistema y nunca se imprime. Gasta créditos del plan del usuario
(1 por carácter con eleven_v3): se genera una vez y solo se regeneran las escenas con fallos.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paper_stage import core  # noqa: E402
from paper_stage.eleven import MODELS, ElevenError, ElevenLabs  # noqa: E402

MAX_CHARS = 1000  # una escena nunca llega a esto; un texto así es un error al armar la lista


def main() -> int:
    parser = argparse.ArgumentParser(description="Voz con ElevenLabs.")
    parser.add_argument("lista", type=Path)
    parser.add_argument("--voz", required=True, help="ID de la voz de ElevenLabs de la serie")
    parser.add_argument("--modelo", default="eleven_v3", choices=sorted(MODELS))
    args = parser.parse_args()
    items = json.loads(args.lista.read_text(encoding="utf-8"))
    for item in items:
        if len(item["texto"]) > MAX_CHARS:
            raise SystemExit(f"Texto de más de {MAX_CHARS} caracteres: divídelo por escenas: {item['texto'][:60]}…")
    eleven = ElevenLabs(sqlite3.connect(core.DATA_DIR / "paper_stage.db"), core.DATA_DIR)
    spent = 0
    for n, item in enumerate(items, 1):
        target = Path(item["salida"])
        match = re.search(r"(\d+)\.wav$", target.name)
        scene = int(match.group(1)) if match else n
        began = time.time()
        try:
            words = eleven.synthesize(item["texto"], args.voz, args.modelo, target, scene)
        except ElevenError as exc:
            print(f"ElevenLabs falló en {target.name}: {exc}", file=sys.stderr)
            return 2
        target.with_suffix(".words.json").write_text(json.dumps(words, ensure_ascii=False, indent=0), encoding="utf-8")
        spent += round(len(item["texto"]) * MODELS[args.modelo])
        last = words[-1]["end"] if words else 0
        print(f"{target}: {len(words)} palabras, voz hasta {last:.1f} s, en {time.time() - began:.0f} s", flush=True)
    print(f"Créditos gastados: unos {spent}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
