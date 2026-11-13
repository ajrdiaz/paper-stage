"""Cuenta las palabras de la voz antes de generarla y, después, compara cada WAV con su texto como lo hace la revisión de la app.

    .venv/bin/python scripts/revisar_voz.py output/<slug>/audio/lista_voz.json --max-palabras 125 --solo-contar
    .venv/bin/python scripts/revisar_voz.py output/<slug>/audio/lista_voz.json

lista_voz.json es la misma lista que usa scripts/voz.py: [{"texto": "...", "salida": "...wav"}, ...].
Whisper también se equivoca: una diferencia es para escucharla o probar otra grafía, no una sentencia.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paper_stage.review import compare_speech  # noqa: E402


def count_words(text: str) -> int:
    return len([w for w in re.split(r"[\s\-–—]+", text) if re.search(r"\w", w)])


def main() -> int:
    parser = argparse.ArgumentParser(description="Cuenta y comprueba la voz de un video.")
    parser.add_argument("lista", type=Path)
    parser.add_argument("--max-palabras", type=int, default=0, help="avisa si el total pasa de este número")
    parser.add_argument("--solo-contar", action="store_true", help="no transcribe: solo cuenta las palabras")
    args = parser.parse_args()
    items = json.loads(args.lista.read_text(encoding="utf-8"))
    total = sum(count_words(item["texto"]) for item in items)
    print(f"Palabras: {total}" + (f" (máximo {args.max_palabras})" if args.max_palabras else ""))
    too_many = bool(args.max_palabras and total > args.max_palabras)
    if too_many:
        print(f"Sobran {total - args.max_palabras}: acorta el texto antes de generar la voz.")
    if args.solo_contar:
        return 1 if too_many else 0

    from faster_whisper import WhisperModel

    # El mismo modelo que la revisión de la app, cargado una sola vez para todas las escenas.
    model = WhisperModel("small", device="cpu", compute_type="int8")
    pending = 0
    for item in items:
        wav = Path(item["salida"])
        if not wav.is_absolute():
            wav = ROOT / wav
        if not wav.exists():
            print(f"{wav.name}: no existe")
            pending += 1
            continue
        segments, _ = model.transcribe(str(wav), word_timestamps=True)
        words = [{"word": w.word.strip(), "start": float(w.start)} for s in segments for w in s.words]
        differences = compare_speech(item["texto"], words)
        if differences:
            pending += 1
            shown = "; ".join(f"«{d['esperado']}» → «{d['oido']}» ({d['t']} s)" for d in differences)
            print(f"{wav.name}: {shown}")
        else:
            print(f"{wav.name}: coincide")
    return 1 if pending or too_many else 0


if __name__ == "__main__":
    raise SystemExit(main())
