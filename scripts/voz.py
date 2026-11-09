"""Genera la voz de un video con Chatterbox. Se ejecuta con el entorno aparte, porque exige otras versiones:

    .venv-voz/bin/python scripts/voz.py lista.json --idioma es

lista.json: [{"texto": "...", "salida": "output/<slug>/audio/raw_01.wav"}, ...], una entrada por escena.
Los ajustes son los de la versión elegida de oído: más estable y con Dora como referencia.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("HF_HOME", str(ROOT / "models" / "hf"))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import torch  # noqa: E402
from chatterbox.mtl_tts import ChatterboxMultilingualTTS  # noqa: E402

# En un Mac con M1, la CPU resultó unas 3 veces más rápida que la GPU.
SETTINGS = {"cfg_weight": 0.5, "exaggeration": 0.5, "temperature": 0.6}
SEED = 7
MAX_CHARS = 320  # el modelo corta el audio largo: más texto debe ir en otra entrada


def main() -> int:
    parser = argparse.ArgumentParser(description="Voz con Chatterbox.")
    parser.add_argument("lista", type=Path)
    parser.add_argument("--idioma", default="es")
    args = parser.parse_args()
    items = json.loads(args.lista.read_text(encoding="utf-8"))
    for item in items:
        if len(item["texto"]) > MAX_CHARS:
            raise SystemExit(f"Texto de más de {MAX_CHARS} caracteres: divídelo en dos entradas: {item['texto'][:60]}…")
    reference = ROOT / "assets" / "voz" / f"referencia-{args.idioma}.wav"
    model = ChatterboxMultilingualTTS.from_pretrained(device="cpu")
    for item in items:
        torch.manual_seed(SEED)
        began = time.time()
        wav = model.generate(item["texto"], language_id=args.idioma,
                             audio_prompt_path=str(reference) if reference.exists() else None, **SETTINGS)
        audio = wav.squeeze().cpu().numpy().astype(np.float32)
        target = Path(item["salida"])
        target.parent.mkdir(parents=True, exist_ok=True)
        sf.write(target, audio, model.sr)
        print(f"{target}: {len(audio) / model.sr:.1f} s en {time.time() - began:.0f} s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
