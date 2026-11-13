"""Renderiza stage.html a final.mp4: WebGL en la GPU del Mac en 3 procesos y, si algún fotograma sale con un cuadro negro, lo rehace por software.

    .venv/bin/python scripts/render_stage.py output/<slug>
    .venv/bin/python scripts/render_stage.py output/<slug> --solo-frames   # sin codificar final.mp4

Con la GPU (Metal), un shader que da NaN (normalize de un vector nulo, pow con base negativa, una división por cero)
pinta un cuadro negro; por software sale bien. Por eso se revisa cada fotograma. Los frames ya hechos se saltan,
así que se puede volver a ejecutar si se corta. Lee la duración de timeline.json y el formato de script.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

GPU_ARGS = ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"]
SOFTWARE_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
SIZES = {"vertical": (1080, 1920), "horizontal": (1920, 1080)}
WORKERS = 3  # más procesos no caben bien en 8 GB de memoria
FPS = 24
EDGE = 25  # largo mínimo, en la imagen reducida a un cuarto (100 px reales), de cada borde recto del cuadro negro


def video_size(out_dir: Path) -> tuple[int, int]:
    try:
        formato = json.loads((out_dir / "script.json").read_text(encoding="utf-8")).get("formato")
    except (OSError, ValueError):
        formato = None
    return SIZES.get(formato or "vertical", SIZES["vertical"])


def frame_count(out_dir: Path) -> int:
    return int(json.loads((out_dir / "timeline.json").read_text(encoding="utf-8"))["duration"] * FPS)


def frame_path(out_dir: Path, i: int) -> Path:
    return out_dir / "frames" / f"{i:05d}.png"


def capture(out_dir: Path, frames: list[int], args: list[str]) -> str:
    """Captura esos frames en un navegador y devuelve el renderer de WebGL que usó."""
    width, height = video_size(out_dir)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=args)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto((out_dir / "stage.html").resolve().as_uri())
        page.evaluate("document.fonts.ready.then(() => 1)")
        renderer = page.evaluate("""(() => { const gl = document.createElement('canvas').getContext('webgl');
            const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
            return ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : String(gl); })()""")
        for i in frames:
            target = frame_path(out_dir, i)
            if target.exists():
                continue
            # renderAt no devuelve nada: serializar una timeline de GSAP cuelga evaluate.
            page.evaluate("t => { window.renderAt(t); }", i / FPS)
            page.screenshot(path=str(target))
        browser.close()
    return renderer


def _longest_runs(edge: np.ndarray) -> int:
    """La racha más larga de True seguidos a lo largo del eje 0, en cualquier columna."""
    run = np.zeros(edge.shape[1], dtype=int)
    best = 0
    for row in edge:
        run = np.where(row, run + 1, 0)
        best = max(best, int(run.max(initial=0)))
    return best


def has_black_square(image: np.ndarray) -> bool:
    """El fallo de la GPU es un cuadrado de negro puro con bordes rectos y nítidos. Un fondo oscuro de verdad se funde poco a poco
    y no lo dispara. image: RGB reducido a un cuarto."""
    peak = image.max(axis=2).astype(int)
    black, lit = peak < 3, peak >= 10
    # Borde vertical: negro con algo encendido 2 px a un lado, durante muchas filas seguidas (y lo mismo en horizontal).
    left = _longest_runs(black[:, 2:] & lit[:, :-2])
    right = _longest_runs(black[:, :-2] & lit[:, 2:])
    top = _longest_runs((black[2:, :] & lit[:-2, :]).T)
    bottom = _longest_runs((black[:-2, :] & lit[2:, :]).T)
    return (left >= EDGE and right >= EDGE) or (top >= EDGE and bottom >= EDGE)


def frame_is_broken(path: Path) -> bool:
    return has_black_square(np.asarray(Image.open(path).convert("RGB").reduce(4)))


def run_workers(out_dir: Path, args: list[str], frames: list[int]) -> None:
    chunks = [frames[k * len(frames) // WORKERS:(k + 1) * len(frames) // WORKERS] for k in range(WORKERS)]
    procs = []
    for chunk in chunks:
        if chunk:
            procs.append(subprocess.Popen([sys.executable, __file__, str(out_dir), "--worker", json.dumps(args),
                                           "--frames", ",".join(map(str, chunk))]))
    for proc in procs:
        proc.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description="Render de stage.html a final.mp4.")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--solo-frames", action="store_true", help="no codifica final.mp4")
    parser.add_argument("--software", action="store_true", help="todo por software, sin GPU")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--frames", help=argparse.SUPPRESS)
    opts = parser.parse_args()
    out_dir = opts.out_dir.resolve()

    if opts.worker:  # proceso hijo: captura su tramo
        capture(out_dir, [int(x) for x in opts.frames.split(",")], json.loads(opts.worker))
        return 0

    total = frame_count(out_dir)
    (out_dir / "frames").mkdir(exist_ok=True)
    began = time.time()
    args = SOFTWARE_ARGS
    if not opts.software:
        renderer = capture(out_dir, [], GPU_ARGS)
        if "Metal" in renderer:
            args = GPU_ARGS
        else:
            print(f"Sin GPU ({renderer}): render por software, unas 2,5 veces más lento.", flush=True)
    for attempt in range(3):  # si un navegador se cae, se reintentan los que falten
        missing = [i for i in range(total) if not frame_path(out_dir, i).exists()]
        if not missing:
            break
        print(f"{total} frames, faltan {len(missing)} ({'GPU' if args is GPU_ARGS else 'software'})", flush=True)
        run_workers(out_dir, args, missing)
    missing = [i for i in range(total) if not frame_path(out_dir, i).exists()]
    if missing:
        raise SystemExit(f"Faltan {len(missing)} frames: vuelve a ejecutarlo.")

    # El detector también salta con algún borde recto sobre fondo negro de verdad: rehacerlo por software cuesta poco y deja el frame bien en ambos casos.
    suspects = [i for i in range(total) if frame_is_broken(frame_path(out_dir, i))]
    if suspects and args is GPU_ARGS:
        print(f"{len(suspects)} frames con un posible cuadro negro de la GPU: se rehacen por software.", flush=True)
        for i in suspects:
            frame_path(out_dir, i).unlink()
        run_workers(out_dir, SOFTWARE_ARGS, suspects)
        if any(not frame_path(out_dir, i).exists() for i in suspects):
            raise SystemExit("Faltan frames rehechos por software: vuelve a ejecutarlo.")
    print(f"Frames listos en {time.time() - began:.0f} s.", flush=True)

    if opts.solo_frames:
        return 0
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(out_dir / "frames" / "%05d.png"),
                    "-i", str(out_dir / "mix.wav"), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium",
                    "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", "-movflags", "+faststart",
                    str(out_dir / "final.mp4")], check=True)
    print("final.mp4 listo.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
