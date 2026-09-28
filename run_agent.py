#!/usr/bin/env python3
"""Teatrito de Papel: lanza el agente desde la terminal (la app web usa el mismo código).

Ejemplos:
    python run_agent.py --tema "Radiación de Hawking" --edad 6-9
    python run_agent.py --personajes                  # lista los personajes (se crean en la app web)
    python run_agent.py --personaje coral --tema "¿Por qué brillan los peces abisales?"
    python run_agent.py --tema "Hawking radiation" --idioma en --formato horizontal
    python run_agent.py --check                       # verifica dependencias locales
    python run_agent.py --tema "Volcanes" --dry-run   # imprime el prompt final
    python run_agent.py --tema "Volcanes" --resume <session_id>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from paper_stage import core, jobs, library


def print_check() -> bool:
    report = core.check_env()
    for kind, title in (("bin", "Binarios"), ("module", "Módulos de Python")):
        print(f"{title}:")
        for item in (i for i in report["items"] if i["kind"] == kind):
            mark = "✓" if item["ok"] else ("·" if item["optional"] else "✗")
            print(f"  {mark} {item['name']:<16} {item['why']}")
    print(f"Fuentes en assets/fonts/: {', '.join(report['fonts']) or '(ninguna; el agente las descargará)'}")
    missing = [n for n in report["vendor_expected"] if n not in report["vendor"]]
    print(f"GSAP en assets/vendor/: {'completo' if not missing else 'falta ' + ', '.join(missing) + ' (ejecuta ./setup.sh; sin él, el agente anima a mano)'}")
    print("\nTodo listo." if report["ok"] else "\nFaltan dependencias: ejecuta ./setup.sh")
    return report["ok"]


async def run(req: core.VideoRequest, resume: str | None) -> int:
    started = time.monotonic()
    ok = False
    async for event in core.run_agent(req, resume_session=resume):
        secs = int(time.monotonic() - started)
        stamp = f"[{secs // 60:02d}:{secs % 60:02d}]"
        if event["kind"] == "text":
            print(f"{stamp} {event['text']}", flush=True)
        elif event["kind"] == "tool":
            print(f"{stamp}   → {event['tool']}: {event['detail']}", flush=True)
        elif event["kind"] == "session":
            print(f"{stamp} Sesión: {event['session_id']} (usa --resume para continuarla)", flush=True)
        elif event["kind"] == "result":
            cost = f"${event['cost_usd']:.2f}" if event["cost_usd"] is not None else "n/d"
            print(f"\n{stamp} Fin ({event['subtype']}): {event['turns']} turnos, costo {cost}.")
            ok = event["ok"]
    final = req.out_dir / "final.mp4"
    cover = library.cover(req.out_dir) if ok and final.exists() else None
    print(f"Log: {(req.out_dir / 'agent_log.jsonl').relative_to(core.ROOT)}")
    print(f"Video: {final.relative_to(core.ROOT) if final.exists() else 'NO se generó final.mp4'}")
    if cover:
        print(f"Portada: {cover.relative_to(core.ROOT)}")
    return 0 if ok and final.exists() else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Agente «Teatrito de Papel» (videos de 60–65 s).")
    parser.add_argument("--tema", help="Tema del video, en el idioma del video.")
    parser.add_argument("--personaje", default=core.LIA.id,
                        help=f"ID del personaje que presenta el video (por defecto: {core.LIA.id}).")
    parser.add_argument("--edad", default="", help="Rango de edad (por defecto: el del personaje).")
    parser.add_argument("--idioma", choices=core.LANGUAGES, default="",
                        help="Idioma del video (por defecto: el idioma principal del personaje).")
    parser.add_argument("--formato", choices=core.FORMATS, default="vertical")
    parser.add_argument("--cta", default="", help="Frase final que dice el personaje (por defecto: la suya).")
    parser.add_argument("--slug", default="", help="Carpeta de salida (por defecto: tema + idioma).")
    parser.add_argument("--model", default=core.DEFAULT_MODEL, help=f"Modelo (por defecto: {core.DEFAULT_MODEL}).")
    parser.add_argument("--effort", choices=core.EFFORTS, default="high")
    parser.add_argument("--max-turns", type=int, default=400)
    parser.add_argument("--max-budget-usd", type=float, default=None, help="Tope de gasto del agente.")
    parser.add_argument("--resume", metavar="SESSION_ID", help="Continúa una sesión interrumpida.")
    parser.add_argument("--dry-run", action="store_true", help="Solo imprime el system prompt final.")
    parser.add_argument("--check", action="store_true", help="Verifica dependencias y sale.")
    parser.add_argument("--personajes", action="store_true", help="Lista los personajes y sale.")
    args = parser.parse_args()

    if args.check:
        return 0 if print_check() else 1
    manager = jobs.JobManager(core.DATA_DIR)
    if args.personajes:
        for c in manager.characters.list():
            print(f"{c.id:<16} {c.nombre} · {c.nicho} · {c.idioma} · {c.edad} años")
        return 0
    if not args.tema:
        parser.error("--tema es obligatorio (salvo con --check o --personajes)")

    req = core.VideoRequest(
        tema=args.tema, edad=args.edad, idioma=args.idioma, formato=args.formato,
        slug=args.slug, cta=args.cta, model=args.model, effort=args.effort,
        max_turns=args.max_turns, max_budget_usd=args.max_budget_usd,
    )
    try:
        manager.prepare(req, args.personaje)
        prompt = core.render_prompt(req.variables())
    except ValueError as exc:
        parser.error(str(exc))
    if args.dry_run:
        print(prompt)
        return 0
    if not args.resume and manager.is_used(req.tema, req.idioma, req.character.id):
        parser.error(f"{req.character.nombre} ya tiene un video sobre «{req.tema}» en este idioma; los temas "
                     "no se repiten (usa --resume para continuar uno interrumpido).")

    print(f"{req.character.nombre} · Tema: {req.tema} · {req.edad} años · {req.idioma} · {req.formato} "
          f"→ output/{req.slug}/")
    return asyncio.run(run(req, args.resume))


if __name__ == "__main__":
    sys.exit(main())
