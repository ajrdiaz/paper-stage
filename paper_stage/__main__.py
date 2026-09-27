"""python -m paper_stage — inicia la aplicación web."""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys


def _bind(host: str, port: int) -> socket.socket:
    """Reserva el socket de escucha; con port=0 el sistema elige uno libre al azar."""
    family = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][0]
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind((host, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


def main() -> int:
    parser = argparse.ArgumentParser(description="Aplicación web de Teatrito de Papel.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Dirección de escucha (por defecto solo este equipo).")
    parser.add_argument("--port", type=int, default=0,
                        help="Puerto de escucha (por defecto uno libre elegido al azar).")
    parser.add_argument("--demo", action="store_true",
                        help="Simula el agente (sin Claude, sin costo) para probar la interfaz.")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="Videos que se producen a la vez (el render usa mucha CPU).")
    args = parser.parse_args()

    if args.demo:
        os.environ["PAPER_STAGE_DEMO"] = "1"
    os.environ["PAPER_STAGE_CONCURRENCY"] = str(args.concurrency)

    local = args.host in ("127.0.0.1", "localhost", "::1")
    if not local and not os.environ.get("PAPER_STAGE_TOKEN"):
        os.environ["PAPER_STAGE_TOKEN"] = secrets.token_urlsafe(18)
        print("La app queda expuesta en la red y el agente ejecuta comandos sin confirmación,")
        print("así que se generó un token de acceso para esta sesión.")
    try:
        sock = _bind(args.host, args.port)
    except OSError as exc:
        print(f"No se pudo escuchar en {args.host}:{args.port} ({exc.strerror or exc}).", file=sys.stderr)
        print("Omite --port para usar uno libre al azar, o elige otro.", file=sys.stderr)
        return 1
    port = sock.getsockname()[1]

    token = os.environ.get("PAPER_STAGE_TOKEN")
    shown = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    if ":" in shown:
        shown = f"[{shown}]"
    url = f"http://{shown}:{port}/" + (f"?token={token}" if token else "")
    print(f"Teatrito de Papel{' (modo demo)' if args.demo else ''} → {url}", flush=True)

    import uvicorn
    from .web import create_app

    # timeout_graceful_shutdown: las conexiones de registro en vivo (SSE) no deben
    # impedir que el apagado llegue a cerrar los agentes en curso.
    config = uvicorn.Config(create_app(), log_level="warning", timeout_graceful_shutdown=5)
    uvicorn.Server(config).run(sockets=[sock])
    return 0


if __name__ == "__main__":
    sys.exit(main())
