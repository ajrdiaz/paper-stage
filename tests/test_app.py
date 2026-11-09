"""Pruebas de la app: python -m unittest discover tests

Usan el agente de demostración (sin Claude) y directorios temporales.
"""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="paper-stage-test-"))
os.environ["PAPER_STAGE_OUTPUT"] = str(TMP / "output")
os.environ["PAPER_STAGE_DATA"] = str(TMP / "data")
os.environ["PAPER_STAGE_DEMO_DELAY"] = "0.01"

from starlette.testclient import TestClient  # noqa: E402

from paper_stage import core, library  # noqa: E402
from paper_stage.demo import (LINES, analyze_demo, demo_account_search, demo_radar_source,  # noqa: E402
                              find_niches_demo, make_ideas_demo, run_demo)
from paper_stage.jobs import JobManager  # noqa: E402
from paper_stage import publishing, radar, review  # noqa: E402

# Las pruebas nunca deben escribir en el Google Drive real de quien las ejecuta.
publishing.drive_roots = lambda: []
core.chatterbox_ready = lambda: False  # el prompt no debe depender de lo instalado en cada equipo
from paper_stage.web import create_app  # noqa: E402


async def slow_runner(req, *, resume_session=None, continuing=False, on_stderr=None):
    yield {"kind": "session", "session_id": "slow-session"}
    if continuing:
        async for event in run_demo(req, resume_session=resume_session, continuing=True):
            yield event
        return
    on_stderr and on_stderr("arrancando CLI")
    await asyncio.sleep(30)


CORAL = {
    "nombre": "Coral", "nicho": "Animales del océano profundo", "idioma": "en",
    "estilo": "Animación oscura y luminosa del fondo del mar, con criaturas translúcidas.",
    "tono": "Curioso y directo, con humor ligero.",
    "paleta": ["#2b4c7e", "#6FB7B7", "#B58BD6", "#12213D"],
    "voces": {"es": "ef_dora", "en": "af_sky"}, "voz_estilo": "juvenil y susurrada",
    "cta": {"en": "Follow me to dive deeper!"},
}


def wait_for(client, job_id, statuses, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(f"el trabajo no llegó a {statuses}: {job['status']}")


class CoreTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(core.slugify("Radiación de Hawking-es"), "radiacion-de-hawking-es")
        self.assertEqual(core.slugify("¿¡!?"), "video")

    def test_render_prompt_fills_all_variables(self):
        req = core.VideoRequest(tema="Los volcanes", idioma="en", formato="horizontal")
        req.validate()
        prompt = core.render_prompt(req.variables())
        self.assertNotIn("{{", prompt)
        self.assertIn("output/los-volcanes-en/", prompt)
        self.assertIn("formato `horizontal`", prompt)

    def test_validation(self):
        for bad in (dict(tema="x"), dict(tema="Volcanes", idioma="fr"), dict(tema="Volcanes", max_budget_usd=0.1),
                    dict(tema="Volcanes", notas="x" * 1501)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                core.VideoRequest(**bad).validate()

    def test_cta(self):
        req = core.VideoRequest(tema="Volcanes", idioma="en")
        req.validate()
        self.assertEqual(req.variables()["CTA"], core.DEFAULT_CTA["en"])
        req = core.VideoRequest(tema="Volcanes", cta="  ¡Nos vemos   pronto!  ")
        req.validate()
        self.assertIn('"¡Nos vemos pronto!"', core.render_prompt(req.variables()))
        with self.assertRaises(ValueError):
            core.VideoRequest(tema="Volcanes", cta="x" * 81).validate()

    def test_default_serie(self):
        req = core.VideoRequest(tema="¿Por qué te da hipo?", notas="  Gancho:   hipo ")
        req.validate()
        self.assertEqual((req.idioma, req.ficha.id), ("es", "ahora-lo-entiendes"))
        prompt = core.render_prompt(req.variables())
        for text in ("de la serie «Ahora lo entiendes»", "Kokoro `ef_dora` (lang_code `e`)", '"serie": "ahora-lo-entiendes"',
                     "#ahoraloentiendes", "primer video de «Ahora lo entiendes»", "gsap.timeline({ paused: true })",
                     "`renderAt` **no devuelve nada**", "Gancho: hipo", "Sin presentador", "público general adulto"):
            self.assertIn(text, prompt)
        # Ya no es para niños ni de papel: el prompt no debe arrastrar nada de eso.
        for text in ("recortes de papel", "gouache", "crayón", "hecho_para_ninos", "Hecho para niños", "años"):
            self.assertNotIn(text, prompt)

    def test_custom_serie_prompt(self):
        serie = core.Serie(**CORAL)
        serie.id = "coral"
        serie.validate()
        req = core.VideoRequest(tema="Why do anglerfish glow?", serie=serie.to_dict(), referencias=["old-coral-video-en"])
        req.validate()
        # Toma el idioma y la frase final de la serie.
        self.assertEqual((req.idioma, req.cta), ("en", "Follow me to dive deeper!"))
        prompt = core.render_prompt(req.variables())
        self.assertNotIn("Ahora lo entiendes", prompt)
        self.assertNotIn("{{", prompt)
        for text in ("de la serie «Coral»", "Animales del océano profundo", "Kokoro `af_sky` (lang_code `a`)",
                     "`#2B4C7E`", "#coral", '"serie": "coral"', "`output/old-coral-video-en/`", "criaturas translúcidas"):
            self.assertIn(text, prompt)
        self.assertIn("full video of the series “Coral”", core.kickoff_prompt(req))
        self.assertIn("No copies sus figuras humanas", prompt)  # los videos antiguos no deben devolver el muñeco

    def test_body_model_is_available_and_free_to_use(self):
        model = core.ROOT / "assets" / "modelos" / "cuerpo-torso.js"
        self.assertIn("window.CUERPO_TORSO", model.read_text())
        self.assertLess(model.stat().st_size, 2_000_000)  # se incrusta en cada stage.html
        self.assertIn("CC0", (model.parent / "LICENCIA.txt").read_text())
        req = core.VideoRequest(tema="El hipo")
        req.validate()
        self.assertIn("assets/modelos/cuerpo-torso.js", core.render_prompt(req.variables()))

    def test_voice_engine_in_the_prompt(self):
        original = core.chatterbox_ready
        core.chatterbox_ready = lambda: True
        try:
            req = core.VideoRequest(tema="El hipo", idioma="es")
            req.validate()
            prompt = core.render_prompt(req.variables())
            self.assertIn("scripts/voz.py output/el-hipo-es/audio/lista_voz.json --idioma es", prompt)
            self.assertIn("Solo si el script falla de verdad, usa Kokoro `ef_dora`", prompt)
            self.assertNotIn("{{", prompt)
            english = core.VideoRequest(tema="Hiccups", idioma="en")  # sin voz de referencia en inglés todavía
            english.validate()
            self.assertNotIn("scripts/voz.py", core.render_prompt(english.variables()))
        finally:
            core.chatterbox_ready = original

    def test_duration_changes_the_prompt(self):
        for duracion, scenes, text, absent in (("corta", "5", "30.0 a 45.0 segundos", "61.0"), ("larga", "7", "61.0 a 70.0 segundos", "30.0 a")):
            with self.subTest(duracion=duracion):
                req = core.VideoRequest(tema="El hipo", duracion=duracion)
                req.validate()
                prompt = core.render_prompt(req.variables())
                self.assertNotIn("{{", prompt)
                self.assertIn(text, prompt)
                self.assertIn(f"con {scenes} escenas y una línea", prompt)
                self.assertIn(f'"duracion": "{duracion}"', prompt)
                self.assertNotIn(absent, prompt)
                self.assertEqual(prompt.count(core.DEFAULT_CTA["es"]) >= 2, True)  # el CTA de dentro de la estructura también se rellena
        with self.assertRaises(ValueError):
            core.VideoRequest(tema="El hipo", duracion="eterna").validate()
        self.assertEqual(core.VideoRequest(tema="El hipo").duracion, "corta")

    def test_script_serie(self):
        self.assertEqual(library.script_serie({}), core.DEFAULT_SERIE.id)
        self.assertEqual(library.script_serie({"serie": "Ahora lo entiendes"}), core.DEFAULT_SERIE.id)  # nombre en vez de ID
        self.assertEqual(library.script_serie({"serie": "coral-2"}), "coral-2")

    def test_serie_validation(self):
        for bad in (dict(paleta=["#FFF", "#000000", "#123456"]), dict(paleta=["#000000"] * 2),
                    dict(voces={"es": "af_heart", "en": "af_sky"}), dict(nicho="x"), dict(estilo="corto"),
                    dict(idioma="fr"), dict(cta={"es": "x" * 81})):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                core.Serie(**{**CORAL, **bad}).validate()

    def test_copied_share(self):
        source = "los mosquitos te pican a ti porque tu piel huele a la cena y la persona de al lado no"
        self.assertEqual(library.copied_share("algo totalmente distinto sobre el hipo y el diafragma", source), 0)
        self.assertEqual(library.copied_share(source, source), 1)
        # Palabras sueltas en común no son copiar: hacen falta tramos largos.
        self.assertEqual(library.copied_share("los mosquitos pican y tu piel huele raro", source), 0)
        self.assertEqual(library.copied_share("", source), 0)

    def test_resumed_cost_is_not_counted_twice(self):
        def result(cost, out, session="s1"):
            return {"type": "ResultMessage", "session_id": session, "total_cost_usd": cost,
                    "model_usage": {"opus": {"inputTokens": 10, "outputTokens": out,
                                             "cacheReadInputTokens": out * 30}}}
        first = result(4.48, 117281)
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "agent_log.jsonl"
            log.write_text("\n".join(json.dumps(r) for r in (first, result(1, 5, "otra"))) + "\n")
            self.assertEqual(core._last_result(log, "s1"), first)
        self.assertTrue(core._includes(result(6.15, 125875), first))   # arrastra lo anterior
        self.assertFalse(core._includes(result(1.67, 8594), first))    # solo esta ejecución


class AppTests(unittest.TestCase):
    def make_client(self, runner=run_demo, token=None, data="data"):
        manager = JobManager(TMP / data, runner=runner)
        return TestClient(create_app(manager, token=token or ""), base_url="http://127.0.0.1")

    def test_other_sites_cannot_use_the_app(self):
        preview = {"tema": "Los volcanes"}
        with self.make_client(data="data-guard") as client:
            evil = {"Origin": "https://evil.example"}
            self.assertEqual(client.post("/api/jobs", json=preview, headers=evil).status_code, 403)
            # Un POST text/plain no pasa por el preflight de CORS.
            self.assertEqual(client.post("/api/jobs", content=json.dumps(preview), headers={
                **evil, "Content-Type": "text/plain"}).status_code, 403)
            self.assertEqual(client.post("/api/jobs", json=preview, headers={"Origin": "null"}).status_code, 403)
            self.assertEqual(client.get("/api/jobs").json(), [])
            self.assertEqual(client.get("/api/config", headers={"Host": "evil.example"}).status_code, 403)
            # La propia interfaz sigue funcionando.
            res = client.post("/api/prompt-preview", json=preview, headers={"Origin": "http://127.0.0.1"})
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(client.get("/api/config", headers={"Host": "localhost:8000"}).status_code, 200)
            self.assertEqual(client.get("/api/config", headers={"Host": "[::1]:8000"}).status_code, 200)
        # Expuesta en la red, el Host puede ser cualquiera: la protege el token.
        with self.make_client(token="secreto", data="data-guard") as client:
            res = client.get("/api/config", headers={"Host": "192.168.1.5:8000", "Authorization": "Bearer secreto"})
            self.assertEqual(res.status_code, 200)

    def test_check_env_reports_the_browser(self):
        self.assertIn("chromium", {i["name"] for i in core.check_env()["items"]})

    def test_full_demo_job(self):
        with self.make_client() as client:
            res = client.post("/api/jobs", json={"tema": "Los delfines", "idioma": "es"})
            self.assertEqual(res.status_code, 201, res.text)
            job = wait_for(client, res.json()["id"], {"done", "failed"})
            self.assertEqual(job["status"], "done", job["error"])
            self.assertTrue(all(s["done"] for s in job["steps"]))

            # Los temas no se repiten (aunque cambien mayúsculas, tildes o signos)…
            res = client.post("/api/jobs", json={"tema": "¡Los DELFINES!", "idioma": "es"})
            self.assertEqual(res.status_code, 400, res.text)
            # …pero uno parecido sí vale.
            again = client.post("/api/jobs", json={"tema": "Los delfines rosados", "idioma": "es"})
            self.assertEqual(again.status_code, 201, again.text)
            wait_for(client, again.json()["id"], {"done", "failed"})

            videos = {v["slug"]: v for v in client.get("/api/videos").json()}
            self.assertIn("los-delfines-es", videos)
            detail = client.get("/api/videos/los-delfines-es").json()
            self.assertEqual(len(detail["json"]["script.json"]["escenas"]), 5)
            self.assertEqual(detail["jobs"][0]["id"], job["id"])
            self.assertEqual([s["key"] for s in detail["steps"]][-4:], ["preqa", "render", "publish", "qa"])
            if shutil.which("ffmpeg"):
                # El video demo es de 540×960: la app lo detecta aunque qa.md no lo diga.
                checks = {c["label"]: c["ok"] for c in detail["verify"]["checks"]}
                self.assertFalse(detail["verify"]["ok"])
                self.assertFalse(checks["Resolución"])
                self.assertTrue(checks["5 hashtags"])
                self.assertTrue(checks["La voz empieza en ≤0.3 s"])  # la voz demo arranca a 0.2 s
                self.assertTrue(checks["Gancho escrito de 6 palabras como máximo"])
                self.assertTrue(checks["Duración entre 30 y 45 s"])
            self.assertEqual(client.get("/files/los-delfines-es/stage.html").status_code, 200)
            self.assertEqual(client.get("/files/los-delfines-es/../../data/paper_stage.db").status_code, 404)
            self.assertEqual(client.get("/files/los-delfines-es/agent_log.jsonl").status_code, 404)
            self.assertEqual(client.get("/api/videos/..%2Fdata").status_code, 404)

            events = client.get(f"/api/jobs/{job['id']}/events").text
            self.assertIn('"kind": "tool"', events)
            if shutil.which("ffmpeg"):
                self.assertIn("Revisión automática de la app, no cumple", events)
            self.assertTrue(events.rstrip().endswith("data: {}"))

            self.assertEqual(client.delete("/api/videos/los-delfines-rosados-es").status_code, 200)
            self.assertFalse((core.OUTPUT_DIR / "los-delfines-rosados-es").exists())

    def test_cancel_and_resume(self):
        with self.make_client(runner=slow_runner, data="data-cancel") as client:
            job = client.post("/api/jobs", json={"tema": "Las abejas"}).json()
            queued = client.post("/api/jobs", json={"tema": "Las hormigas"}).json()
            wait_for(client, job["id"], {"running"})
            self.assertEqual(client.get(f"/api/jobs/{queued['id']}").json()["queue_position"], 1)

            self.assertEqual(client.post(f"/api/jobs/{queued['id']}/cancel").json()["status"], "cancelled")
            client.post(f"/api/jobs/{job['id']}/cancel")
            cancelled = wait_for(client, job["id"], {"cancelled"})
            self.assertEqual(cancelled["session_id"], "slow-session")
            self.assertEqual(client.post(f"/api/jobs/{job['id']}/cancel").status_code, 409)

            client.post(f"/api/jobs/{job['id']}/resume")
            done = wait_for(client, job["id"], {"done", "failed"})
            self.assertEqual(done["status"], "done", done["error"])
            kinds = [e["kind"] for e in client.app.state.manager.events(job["id"])]
            self.assertIn("stderr", kinds)

            self.assertEqual(client.delete(f"/api/jobs/{queued['id']}").status_code, 200)
            self.assertEqual(client.get(f"/api/jobs/{queued['id']}").status_code, 404)

    def test_restart_marks_running_jobs_interrupted(self):
        data = TMP / "data-restart"
        data.mkdir(parents=True, exist_ok=True)
        JobManager(data)  # crea el esquema
        db = sqlite3.connect(data / "paper_stage.db")
        db.execute("INSERT INTO jobs (id, slug, request, status, created_at) VALUES (?, ?, ?, ?, ?)",
                   ("old", "viejo-es", '{"tema": "Viejo"}', "running", time.time()))
        db.commit()
        with self.make_client(data="data-restart") as client:
            job = client.get("/api/jobs/old").json()
            self.assertEqual(job["status"], "interrupted")

    def test_token_auth(self):
        with self.make_client(token="secreto", data="data-token") as client:
            self.assertEqual(client.get("/api/config").status_code, 401)
            ok = client.get("/api/config", headers={"Authorization": "Bearer secreto"})
            self.assertEqual(ok.status_code, 200)
            login = client.get("/?token=secreto", follow_redirects=False)
            self.assertEqual(login.status_code, 307)
            self.assertEqual(client.get("/api/config").status_code, 200)  # cookie

    def test_suggestions_never_repeat(self):
        with self.make_client(data="data-suggest") as client:
            res = client.post("/api/jobs", json={"tema": "¿Por qué el mar es salado?", "idioma": "es"})
            wait_for(client, res.json()["id"], {"done", "failed"})
            shown = []
            for _ in range(2):
                res = client.post("/api/suggest", json={"idioma": "es", "evitar": shown})
                self.assertEqual(res.status_code, 200, res.text)
                temas = [t["tema"] for t in res.json()["temas"]]
                self.assertEqual(len(temas), 3)
                shown += temas
            keys = [core.topic_key(t, "es") for t in shown]
            self.assertEqual(len(set(keys)), 6)
            self.assertNotIn(core.topic_key("¿Por qué el mar es salado?", "es"), keys)

    def test_series(self):
        with self.make_client(data="data-series") as client:
            series = client.get("/api/series").json()
            self.assertEqual([c["id"] for c in series], [core.DEFAULT_SERIE.id])  # siempre hay al menos una
            only = series[0]["id"]
            self.assertEqual(client.delete(f"/api/series/{only}").status_code, 409)  # no se borra la única

            self.assertEqual(client.post("/api/series", json={**CORAL, "paleta": ["#123"]}).status_code, 400)
            self.assertEqual(client.post("/api/series", json={"nombre": "Solo nombre"}).status_code, 400)
            res = client.post("/api/series", json=CORAL)
            self.assertEqual(res.status_code, 201, res.text)
            coral = res.json()
            self.assertEqual((coral["id"], coral["hashtag"]), ("coral", "#coral"))
            self.assertEqual(client.post("/api/series", json=CORAL).json()["id"], "coral-2")

            res = client.post("/api/prompt-preview", json={"tema": "Anglerfish", "serie_id": "coral"})
            self.assertIn("de la serie «Coral»", res.json()["prompt"])
            self.assertEqual(res.json()["slug"], "anglerfish-en")
            self.assertEqual(client.post("/api/prompt-preview", json={"tema": "X y", "serie_id": "nadie"}).status_code, 400)

            topics = client.post("/api/suggest", json={"serie_id": "coral"})
            self.assertEqual(topics.status_code, 200, topics.text)

            # El video lleva la serie y su idioma; el mismo tema no se repite en ella, pero otra sí puede tratarlo.
            job = client.post("/api/jobs", json={"tema": "Las medusas", "serie_id": "coral"}).json()
            self.assertEqual(job["request"]["serie"]["nombre"], "Coral")
            self.assertEqual(client.delete("/api/series/coral").status_code, 409)  # en producción
            job = wait_for(client, job["id"], {"done", "failed"})
            self.assertEqual(job["status"], "done", job["error"])
            video = client.get(f"/api/videos/{job['slug']}").json()
            self.assertEqual((video["serie"], video["idioma"]), ("coral", "en"))
            self.assertIn("Coral", video["texts"]["bible.md"])
            self.assertEqual(client.post("/api/jobs", json={"tema": "las medusas", "serie_id": "coral"}).status_code, 400)
            other = client.post("/api/jobs", json={"tema": "Las medusas", "idioma": "en", "serie_id": only})
            self.assertEqual(other.status_code, 201, other.text)
            wait_for(client, other.json()["id"], {"done", "failed"})

            # Su segundo video copia el diseño del primero y ve su gancho, para no repetir la apertura.
            res = client.post("/api/prompt-preview", json={"tema": "Octopus ink", "serie_id": "coral"})
            self.assertIn(f"`output/{job['slug']}/`", res.json()["prompt"])
            self.assertIn("- pregunta imposible: Did you know that Las medusas", res.json()["prompt"])
            self.assertIn("- (ninguno todavía)", client.post("/api/prompt-preview", json={
                "tema": "Otro tema", "serie_id": "coral-2"}).json()["prompt"])

            listed = {c["id"]: c for c in client.get("/api/series").json()}
            self.assertEqual((listed["coral"]["videos"], listed["coral"]["ultimo_video"]), (1, job["slug"]))
            res = client.put("/api/series/coral", json={**CORAL, "nicho": "Criaturas del arrecife"})
            self.assertEqual(res.json()["nicho"], "Criaturas del arrecife")
            self.assertEqual(client.put("/api/series/nadie", json=CORAL).status_code, 404)
            self.assertIsNone(listed["coral-2"]["canal"])  # las series nuevas empiezan sin cuenta
            client.put("/api/publishing/channels/coral-2", json={"channel_id": "tt2", "channel_name": "@coral"})
            self.assertEqual(client.delete("/api/series/coral-2").status_code, 200)
            self.assertEqual(client.get("/api/series/coral-2").status_code, 404)
            self.assertIsNone(client.app.state.publisher.channel_for("coral-2"))  # su cuenta no queda huérfana

    def test_long_videos_are_still_checked_as_long(self):
        with self.make_client(data="data-duration") as client:
            job = client.post("/api/jobs", json={"tema": "Las ojeras", "duracion": "larga"}).json()
            job = wait_for(client, job["id"], {"done", "failed"})
            video = client.get(f"/api/videos/{job['slug']}").json()
            self.assertEqual(len(video["json"]["script.json"]["escenas"]), 7)
            if shutil.which("ffmpeg"):
                self.assertTrue({c["label"]: c["ok"] for c in video["verify"]["checks"]}["Duración entre 61 y 70 s"])
            # Corregir un video conserva su duración, aunque sea anterior a poder elegirla.
            script = core.OUTPUT_DIR / job["slug"] / "script.json"
            data = json.loads(script.read_text())
            del data["duracion"]
            script.write_text(json.dumps(data))
            fix = client.post(f"/api/videos/{job['slug']}/corrections", json={"correcciones": ["x"]}).json()
            self.assertEqual(fix["request"]["duracion"], "larga")
            wait_for(client, fix["id"], {"done", "failed"})
            self.assertEqual(client.post("/api/jobs", json={"tema": "Otro tema", "duracion": "eterna"}).status_code, 400)

    def test_review_and_corrections(self):
        with self.make_client(data="data-review") as client:
            job = client.post("/api/jobs", json={"tema": "Los dedos"}).json()
            job = wait_for(client, job["id"], {"done", "failed"})
            slug = job["slug"]
            # La revisión corre sola al terminar y sus correcciones llegan a la ficha y a la biblioteca.
            deadline = time.time() + 20
            while time.time() < deadline:
                video = client.get(f"/api/videos/{slug}").json()
                if not video["revisando"] and video["revision"]:
                    break
                time.sleep(0.05)
            self.assertEqual(video["revision"]["sugerencias"][0]["gravedad"], "alta")
            self.assertEqual(video["revision_altas"], 1)
            self.assertEqual(next(v for v in client.get("/api/videos").json() if v["slug"] == slug)["revision_altas"], 1)

            self.assertEqual(client.post(f"/api/videos/{slug}/corrections", json={"correcciones": []}).status_code, 409)
            self.assertEqual(client.post(f"/api/videos/{slug}/corrections", json={"correcciones": "x"}).status_code, 409)
            self.assertEqual(client.post("/api/videos/no-existe/corrections", json={"correcciones": ["x"]}).status_code, 409)
            res = client.post(f"/api/videos/{slug}/corrections", json={"correcciones": ["Agranda   los subtítulos"]})
            self.assertEqual(res.status_code, 201, res.text)
            fix = res.json()
            self.assertEqual((fix["slug"], fix["request"]["correcciones"]), (slug, ["Agranda los subtítulos"]))  # misma carpeta
            self.assertEqual(client.post(f"/api/videos/{slug}/corrections", json={"correcciones": ["y"]}).status_code, 409)  # ya en curso
            prompt = core.kickoff_prompt(core.VideoRequest(**fix["request"]))
            self.assertIn("Corrige SOLO esto", prompt)
            self.assertIn("- Agranda los subtítulos", prompt)
            self.assertIn("backup/", prompt)
            fix = wait_for(client, fix["id"], {"done", "failed"})
            self.assertEqual(fix["status"], "done", fix["error"])
            self.assertFalse((core.OUTPUT_DIR / f"{slug}-2").exists())
            self.assertEqual(client.post("/api/videos/no-existe/review").status_code, 409)

    def test_failed_review_does_not_touch_the_finished_video(self):
        async def broken(out_dir, serie=None):
            raise RuntimeError("sin Whisper")

        with self.make_client(data="data-review-fail") as client:
            client.app.state.manager.reviewer = broken
            job = client.post("/api/jobs", json={"tema": "Las uñas"}).json()
            job = wait_for(client, job["id"], {"done", "failed"})
            self.assertEqual(job["status"], "done")
            deadline = time.time() + 10
            while time.time() < deadline and client.app.state.manager.reviewing(job["slug"]):
                time.sleep(0.02)
            self.assertIn("sin Whisper", client.get(f"/api/videos/{job['slug']}").json()["revision_error"])

    def test_script_is_compared_with_its_sources(self):
        with self.make_client(data="data-similar") as client:
            # El demo usa estas frases en su guion: un guion copiado debe avisarse en la revisión.
            copied = " ".join(line.format(tema="Las pecas", cta="") for line in LINES["es"])
            client.app.state.manager.source_texts = lambda ids: [copied] if ids == ["123456"] else []
            job = client.post("/api/jobs", json={"tema": "Las pecas", "inspiracion": ["123456", "no-vale"]}).json()
            self.assertEqual(job["request"]["inspiracion"], ["123456"])
            job = wait_for(client, job["id"], {"done", "failed"})
            detail = client.get(f"/api/videos/{job['slug']}").json()
            if shutil.which("ffmpeg"):
                checks = {c["label"]: c for c in detail["verify"]["checks"]}
                self.assertFalse(checks["Guion distinto de los videos que lo inspiraron"]["ok"])
            original = client.post("/api/jobs", json={"tema": "Los lunares"}).json()  # sin fuentes, no se compara
            original = wait_for(client, original["id"], {"done", "failed"})
            self.assertFalse((core.OUTPUT_DIR / original["slug"] / "parecido.json").exists())
            self.assertEqual(client.post("/api/jobs", json={"tema": 123}).status_code, 400)

    def test_publish_api_demo(self):
        with self.make_client(data="data-publish") as client:
            self.assertFalse(client.get("/api/publishing").json()["ready"])
            job = client.post("/api/jobs", json={"tema": "Las nubes viajeras"}).json()
            job = wait_for(client, job["id"], {"done", "failed"})
            self.assertEqual(client.post(f"/api/videos/{job['slug']}/publish", json={"text": "Hola"}).status_code, 400)
            self.assertEqual(client.put("/api/publishing", json={"buffer_key": "corta"}).status_code, 400)
            cfg = client.put("/api/publishing", json={"buffer_key": "clave-demo-1234", "create_folder": True,
                                                      "drive_folder": client.get("/api/publishing").json()["drive_folder"]}).json()
            self.assertEqual((cfg["buffer_key"], cfg["buffer_key_hint"]), (True, "…1234"))
            self.assertNotIn("clave-demo-1234", json.dumps(cfg))  # la clave nunca vuelve al navegador
            self.assertTrue(cfg["ready"])
            # Sin cuenta de TikTok asignada a la serie, no se publica.
            info = client.get(f"/api/videos/{job['slug']}/publish").json()
            self.assertEqual((info["serie"], info["channel"]), (core.DEFAULT_SERIE.id, None))
            self.assertIn("no tiene cuenta", client.post(f"/api/videos/{job['slug']}/publish", json={"text": "Hola"}).json()["error"])
            channels = client.get("/api/publishing/channels").json()
            res = client.put(f"/api/publishing/channels/{core.DEFAULT_SERIE.id}", json={"channel_id": channels[0]["id"], "channel_name": channels[0]["nombre"]})
            self.assertEqual(res.json()["canal"], {"id": channels[0]["id"], "nombre": channels[0]["nombre"]})
            self.assertEqual(client.put("/api/publishing/channels/nadie", json={"channel_id": "x"}).status_code, 404)
            serie = client.get(f"/api/series/{core.DEFAULT_SERIE.id}").json()
            self.assertEqual(serie["canal"]["id"], channels[0]["id"])
            info = client.get(f"/api/videos/{job['slug']}/publish").json()
            self.assertEqual(info["channel"]["id"], channels[0]["id"])
            self.assertIn("#", info["default_text"])
            res = client.post(f"/api/videos/{job['slug']}/publish", json={"text": info["default_text"], "mode": "now"})
            self.assertEqual(res.status_code, 200, res.text)
            deadline = time.time() + 20
            while time.time() < deadline:
                state = client.get(f"/api/videos/{job['slug']}/publish").json()
                if state["status"]["state"] != "running":
                    break
                time.sleep(0.1)
            self.assertEqual(state["status"]["state"], "done", state)
            self.assertEqual(len(state["history"]), 1)
            self.assertEqual(state["history"][0]["status"], "sent")  # la ficha pregunta a Buffer al abrirse
            self.assertEqual(next(v for v in client.get("/api/videos").json() if v["slug"] == job["slug"])["tiktok"], "publicado")
            again = client.post(f"/api/videos/{job['slug']}/publish", json={"text": "Otra"})
            self.assertEqual(again.status_code, 409)
            other = client.post("/api/jobs", json={"tema": "Las gotas de lluvia"}).json()
            other = wait_for(client, other["id"], {"done", "failed"})
            res = client.post(f"/api/videos/{other['slug']}/publish/manual")
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(client.post(f"/api/videos/{other['slug']}/publish/manual").status_code, 409)
            flags = {v["slug"]: v["en_tiktok"] for v in client.get("/api/videos").json()}
            self.assertTrue(flags[other["slug"]] and flags[job["slug"]])
            jobs = {j["slug"]: j["en_tiktok"] for j in client.get("/api/jobs").json()}
            self.assertTrue(jobs[other["slug"]] and jobs[job["slug"]])
            self.assertTrue(client.get(f"/api/jobs/{other['id']}").json()["en_tiktok"])
            mark = res.json()["history"][0]["id"]
            self.assertEqual(client.delete(f"/api/videos/{other['slug']}/publish/{mark}").json()["history"], [])
            self.assertEqual(client.get("/api/videos/..%2Fx/publish").status_code, 404)

    def test_prompt_preview_and_errors(self):
        with self.make_client(data="data-preview") as client:
            res = client.post("/api/prompt-preview", json={"tema": "El arcoíris", "idioma": "en"})
            self.assertEqual(res.json()["slug"], "el-arcoiris-en")
            self.assertIn("about “El arcoíris”", res.json()["kickoff"])
            self.assertEqual(client.post("/api/jobs", json={"tema": ""}).status_code, 400)
            self.assertEqual(client.post("/api/jobs", content=b"no-json").status_code, 400)


class FakeNet:
    """Buffer y la descarga de Drive simulados; guarda lo que se envió a Buffer."""

    def __init__(self, public=True, key_ok=True, login=False, post_status="scheduled"):
        self.public, self.key_ok, self.login, self.posts = public, key_ok, login, []
        self.post_status, self.status_queries = post_status, 0

    def __call__(self, method, url, headers, body):
        if url != publishing.BUFFER_API:
            if self.public:
                return 206, {"Content-Type": "video/mp4"}, b"\x00\x00\x00\x18ftypmp42"
            if self.login:  # carpeta sin compartir: Drive redirige a iniciar sesión
                return 200, {"Content-Type": "text/html",
                             "X-Final-URL": "https://accounts.google.com/ServiceLogin?service=wise"}, b"<html>"
            return 200, {"Content-Type": "text/html"}, b"<!doctype html><title>Google Drive</title>"
        if not self.key_ok:
            return 200, {}, json.dumps({"errors": [{"message": "Not authorized",
                                                    "extensions": {"code": "UNAUTHORIZED"}}]}).encode()
        payload = json.loads(body)
        if "createPost" in payload["query"]:
            self.posts.append(payload["variables"]["input"])
            data = {"createPost": {"post": {"id": f"p{len(self.posts)}", "dueAt": "2026-10-01T15:00:00Z",
                                            "status": "scheduled"}}}
        elif "post(input" in payload["query"]:
            self.status_queries += 1
            if self.post_status is None:  # borrado en Buffer
                return 200, {}, json.dumps({"data": None, "errors": [{"message": "Post not found",
                                                                     "extensions": {"code": "NOT_FOUND"}}]}).encode()
            data = {"post": {"id": payload["variables"]["id"], "status": self.post_status,
                             "dueAt": "2026-10-01T15:00:00Z", "sentAt": "2026-09-29T01:00:00Z"}}
        elif "organizations" in payload["query"]:
            data = {"account": {"organizations": [{"id": "o1", "name": "Mía"}]}}
        else:
            data = {"channels": [{"id": "tt1", "displayName": "@teatrito", "service": "tiktok"},
                                 {"id": "ig1", "displayName": "@insta", "service": "instagram"}]}
        return 200, {}, json.dumps({"data": data}).encode()


class PublishingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pub-", dir=TMP))
        self.folder = self.tmp / "drive"
        self.slug = "video-publicable-es"
        out = core.OUTPUT_DIR / self.slug
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 1000)
        (out / "publish.json").write_text(json.dumps({
            "descripcion_corta": "¿Las arañas se pegan? 🕷️", "frame_portada_s": 1.5,
            "hashtags": ["#a", "#b", "#c", "#d", "#e", "#f"]}), encoding="utf-8")

    def publisher(self, net, **kw):
        pub = publishing.Publisher(sqlite3.connect(":memory:"), http=net, poll=0.01,
                                   drive_id=lambda p: "ID123" if p.exists() else None,
                                   demo=True, default_folder=self.folder, **kw)
        pub.save_config({"buffer_key": "clave-de-prueba-123", "drive_folder": str(self.folder),
                         "create_folder": True})
        pub.set_channel(core.DEFAULT_SERIE.id, "tt1", "@teatrito")
        return pub

    async def finish(self, pub, slug):
        await pub.tasks[slug]
        return pub.status[slug]

    def test_helpers(self):
        self.assertEqual(publishing.text_units("🕷️a"), 4)  # un emoji cuenta 2 (más el selector de variante)
        text = publishing.default_text(json.loads((core.OUTPUT_DIR / self.slug / "publish.json").read_text()))
        self.assertEqual(text, "¿Las arañas se pegan? 🕷️\n\n#a #b #c #d #e")  # solo 5 hashtags
        self.assertEqual(publishing.check_video_url("u", FakeNet())[0], "ok")
        self.assertEqual(publishing.check_video_url("u", FakeNet(public=False))[0], "other")
        result, detail = publishing.check_video_url("u", FakeNet(public=False, login=True))
        self.assertEqual(result, "login")
        self.assertIn("Cualquier persona con el enlace", detail)
        self.assertEqual(publishing.public_url("X"),
                         "https://drive.usercontent.google.com/download?id=X&export=download&confirm=t")

    async def test_publish_flow(self):
        net = FakeNet()
        pub = self.publisher(net)
        self.assertTrue(pub.config()["ready"])
        self.assertEqual([c["id"] for c in await pub.channels()], ["tt1"])  # solo TikTok
        pub.start(self.slug, publishing.PublishRequest(text="Hola #a", mode="schedule",
                                                       due_at="2026-10-01T15:00:00Z"))
        st = await self.finish(pub, self.slug)
        self.assertEqual(st["state"], "done", st)
        self.assertTrue((self.folder / f"{self.slug}.mp4").exists())
        post = net.posts[0]
        self.assertEqual((post["channelId"], post["mode"], post["dueAt"]), ("tt1", "customScheduled", "2026-10-01T15:00:00Z"))
        self.assertEqual(post["assets"][0]["video"], {
            "url": publishing.public_url("ID123"), "metadata": {"thumbnailOffset": 1500}})
        self.assertTrue(post["metadata"]["tiktok"]["isAiGenerated"])
        self.assertEqual(pub.history(self.slug)[0]["post_id"], "p1")
        # No se envía dos veces sin confirmarlo.
        with self.assertRaises(publishing.PublishError):
            pub.start(self.slug, publishing.PublishRequest(text="Hola"))
        pub.start(self.slug, publishing.PublishRequest(text="Hola", mode="now", ai_label=False, again=True))
        await self.finish(pub, self.slug)
        self.assertEqual((net.posts[1]["mode"], net.posts[1]["metadata"]["tiktok"]["isAiGenerated"]), ("shareNow", False))

    async def test_channel_per_serie(self):
        net = FakeNet()
        pub = self.publisher(net)
        # Un video de otra serie va a la cuenta de esa serie, nunca a la de otra.
        slug = "video-de-coral-es"
        out = core.OUTPUT_DIR / slug
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 1000)
        (out / "script.json").write_text(json.dumps({"serie": "coral"}), encoding="utf-8")
        with self.assertRaises(publishing.PublishError) as ctx:
            pub.start(slug, publishing.PublishRequest(text="Hola"))
        self.assertIn("no tiene cuenta", str(ctx.exception))
        self.assertEqual(pub.mark_manual(slug)["history"][0]["channel_id"], "")  # sin cuenta: se marca igual
        pub.set_channel("coral", "tt2", "@coral")
        self.assertEqual(pub.video_channel(slug), ("coral", {"id": "tt2", "nombre": "@coral"}))
        pub.start(slug, publishing.PublishRequest(text="Hola", again=True))
        await self.finish(pub, slug)
        self.assertEqual(net.posts[0]["channelId"], "tt2")
        self.assertEqual(pub.history(slug)[0]["channel_name"], "@coral")
        pub.set_channel("coral", "")
        self.assertIsNone(pub.channel_for("coral"))

    async def test_manual_mark(self):
        net = FakeNet()
        pub = self.publisher(net)
        state = pub.mark_manual(self.slug)
        self.assertEqual(state["history"][0]["mode"], "manual")
        self.assertIn(self.slug, pub.published())
        with self.assertRaises(publishing.PublishError):
            pub.mark_manual(self.slug)  # no se marca dos veces
        with self.assertRaises(publishing.PublishError) as ctx:
            pub.start(self.slug, publishing.PublishRequest(text="Hola"))
        self.assertIn("marcado a mano", str(ctx.exception))
        self.assertEqual(net.posts, [])  # no llegó nada a Buffer
        # Una publicación hecha por Buffer no se puede "desmarcar".
        pub.start(self.slug, publishing.PublishRequest(text="Hola", again=True))
        await self.finish(pub, self.slug)
        sent = next(h for h in pub.history(self.slug) if h["mode"] != "manual")
        with self.assertRaises(publishing.PublishError):
            pub.unmark(self.slug, sent["id"])
        manual = next(h for h in pub.history(self.slug) if h["mode"] == "manual")
        self.assertEqual([h["mode"] for h in pub.unmark(self.slug, manual["id"])["history"]], ["queue"])
        with self.assertRaises(publishing.PublishError):
            pub.mark_manual("no-existe-es")

    async def test_refresh_status(self):
        net = FakeNet()
        pub = self.publisher(net)
        pub.start(self.slug, publishing.PublishRequest(text="Hola"))
        await self.finish(pub, self.slug)
        self.assertEqual(pub.published()[self.slug], "programado")  # aún no salió
        net.post_status = "sending"  # «publicar ahora»: Buffer tarda unos minutos en subirlo
        pub.refresh(self.slug)
        self.assertEqual(pub.published()[self.slug], "enviando")
        net.post_status = "sent"  # lo publicaron desde el panel de Buffer
        pub.refresh(self.slug, min_interval=0)
        row = pub.history(self.slug)[0]
        self.assertEqual((row["status"], row["sent_at"]), ("sent", "2026-09-29T01:00:00Z"))
        self.assertEqual(pub.published()[self.slug], "publicado")
        pub.refresh(self.slug, min_interval=0)  # ya es definitivo: no se vuelve a preguntar
        self.assertEqual(net.status_queries, 2)
        # Borrado en Buffer: deja de contar como publicado o programado.
        pub.start(self.slug, publishing.PublishRequest(text="Otra", again=True))
        await self.finish(pub, self.slug)
        net.post_status = None
        pub.refresh(self.slug, min_interval=0)
        self.assertEqual({h["status"] for h in pub.history(self.slug)}, {"sent", "deleted"})
        self.assertEqual(pub.published()[self.slug], "publicado")

    async def test_errors_are_readable(self):
        # Carpeta sin compartir: falla rápido (sin agotar la espera) y dice cómo arreglarlo.
        pub = self.publisher(FakeNet(public=False, login=True), sync_timeout=60, login_grace=0.05)
        started = time.monotonic()
        pub.start(self.slug, publishing.PublishRequest(text="Hola"))
        st = await self.finish(pub, self.slug)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(st["state"], "failed")
        self.assertIn("Cualquier persona con el enlace", st["step"])
        # Drive aún procesando: espera hasta el límite y lo dice.
        pub = self.publisher(FakeNet(public=False), sync_timeout=0.1)
        pub.start(self.slug, publishing.PublishRequest(text="Hola"))
        st = await self.finish(pub, self.slug)
        self.assertIn("aún lo esté procesando", st["step"])
        pub = self.publisher(FakeNet(key_ok=False))
        with self.assertRaises(publishing.PublishError) as ctx:
            await pub.channels()
        self.assertIn("clave de API", str(ctx.exception))
        for bad in (dict(text=""), dict(text="x" * 2201), dict(text="x", mode="schedule"), dict(text="x", mode="luego")):
            with self.subTest(bad=bad), self.assertRaises(publishing.PublishError):
                publishing.PublishRequest(**bad).validate()

    async def test_config_test_run(self):
        pub = self.publisher(FakeNet())
        pub.start(publishing.TEST_SLUG, None)
        st = await self.finish(pub, publishing.TEST_SLUG)
        if shutil.which("ffmpeg"):
            self.assertEqual(st["state"], "done", st)
            self.assertTrue((self.folder / "prueba-publicacion.mp4").exists())

    def test_agent_does_not_see_secrets(self):
        os.environ["PAPER_STAGE_BUFFER_KEY"] = "secreta"
        try:
            self.assertEqual(core.agent_env()["PAPER_STAGE_BUFFER_KEY"], "")
        finally:
            del os.environ["PAPER_STAGE_BUFFER_KEY"]


def radar_row(handle, video_id, views, pinned=False, created=None):
    return {"id": video_id, "handle": handle, "views": views, "pinned": pinned, "duration": 65,
            "created_at": created or time.time() - 86400}


class ReviewTests(unittest.TestCase):
    def heard(self, text, start=0.0):
        return [{"word": w, "start": start + i, "end": start + i + .5, "prob": .9} for i, w in enumerate(text.split())]

    def test_speech_is_compared_with_the_script(self):
        # Lo que pasó de verdad en el primer video: se comió la «E» de «Etiqueta» y cambió una vocal.
        expected = "Etiqueta a quien sale. Sobra piel y se pliega, ¿para qué?"
        diffs = review.compare_speech(expected, self.heard("Tiqueta a quien sale sobre piel y se pliega para que", 10))
        self.assertEqual([(d["esperado"], d["oido"]) for d in diffs], [("etiqueta", "tiqueta"), ("sobra", "sobre")])
        self.assertEqual(diffs[0]["t"], 10.0)
        self.assertEqual(review.compare_speech("¿Para qué?", self.heard("para que")), [])  # tildes y signos no cuentan
        self.assertEqual(review.compare_speech("Hola", []), [{"t": 0.0, "esperado": "hola", "oido": ""}])
        # Falsos avisos del primer video de México: se oyen igual, solo se escriben distinto.
        self.assertEqual(review.compare_speech("de cinco a treinta veces, a quien le ruge en cada junta",
                                               self.heard("de 5 a 30 veces a quien le rujen cada junta")), [])
        self.assertEqual(review.compare_speech("sobra piel", self.heard("sobre piel"))[0]["esperado"], "sobra")  # esto sí es distinto

    def test_measures_become_suggestions(self):
        found = review.measured_suggestions({
            "diferencias": [{"t": 57.7, "esperado": "etiqueta", "oido": "tiqueta"}, {"t": 3.0, "esperado": "", "oido": "eh"}],
            "inseguras": [{"t": 1.0, "word": "Tus", "prob": .11}], "palabras_minuto": 220, "pausas": [(30.0, 2.0)],
            "pico_db": 0.0, "congelados": [(12.0, 4.0)], "negros": [(5.0, 1.0)]})
        kinds = [(s["gravedad"], s["tipo"]) for s in found]
        self.assertIn(("alta", "audio"), kinds)
        self.assertEqual(sum(s["gravedad"] == "alta" for s in found), 4)  # palabra, saturación, congelado y negro
        self.assertTrue(all(s["origen"] == "medicion" and s["correccion"] for s in found))
        self.assertFalse(any("(nada)" in s["problema"] and "«eh»" in s["problema"] for s in found))  # muletilla de Whisper
        self.assertEqual(review.measured_suggestions({"palabras_minuto": 165, "pico_db": -1.0}), [])

    def test_video_filters_are_parsed(self):
        log = ("[freezedetect] lavfi.freezedetect.freeze_start: 12.5\n[freezedetect] lavfi.freezedetect.freeze_duration: 3.2\n"
               "[blackdetect] black_start:1.5 black_end:2 black_duration:0.5\n"
               "lavfi.scene_score=0.4\nlavfi.scene_score=0.5\n")
        self.assertEqual(review.parse_video_filters(log), {"congelados": [(12.5, 3.2)], "negros": [(1.5, 0.5)], "cortes": 2})

    def test_claude_suggestions_are_cleaned(self):
        clean = review.clean_suggestions([
            {"gravedad": "ALTA", "t": "12", "tipo": "Comprensión", "problema": "x", "correccion": "y"},
            {"gravedad": "rara", "t": "nunca", "tipo": "otro", "problema": "z"}, {"problema": ""}, "texto"])
        self.assertEqual([(c["gravedad"], c["t"], c["tipo"]) for c in clean], [("alta", 12.0, "comprension"), ("media", 0.0, "calidad")])
        self.assertEqual(review.clean_suggestions("nada"), [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "hace falta ffmpeg")
    def test_frames_every_few_seconds_and_saved_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, video = Path(tmp), Path(tmp) / "v.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=128x128:r=24:d=10",
                            "-pix_fmt", "yuv420p", str(video)], check=True, capture_output=True)
            frames = review.extract_frames(video, out / "revision")
            self.assertEqual([f["t"] for f in frames], [0, 3, 6, 9])
            self.assertTrue((out / frames[1]["archivo"]).exists())
            measured = review.measure_video(video)
            self.assertEqual(measured["negros"], [])
            saved = review.save(out, {"fotogramas": frames, "sugerencias": [
                {"gravedad": "baja", "t": 1, "problema": "a"}, {"gravedad": "alta", "t": 8, "problema": "b"}]})
            self.assertEqual([(s["id"], s["problema"], s["fotograma"]) for s in saved["sugerencias"]],
                             [("s1", "b", "revision/t009.jpg"), ("s2", "a", "revision/t000.jpg")])


class RadarUnitTests(unittest.TestCase):
    def test_handles_and_counts(self):
        for text in ("https://www.tiktok.com/@ImportaCrew?lang=es", "@importacrew", " importacrew "):
            self.assertEqual(radar.parse_handle(text), "importacrew")
        for bad in ("", "a", "no vale con espacios", "https://evil.example/x y"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                radar.parse_handle(bad)
        self.assertEqual([radar.parse_count(t) for t in ("5.1M", "132,9 K", "8285", "", None)],
                         [5_100_000, 132_900, 8285, 0, 0])
        # El ID de un video guarda su fecha (video real de @nuncateloexplicaron, del 2026-11-07).
        self.assertEqual(time.gmtime(radar.video_time("7705573604842360096"))[:3], (2026, 11, 7))

    def test_ranking_is_arithmetic(self):
        now = time.time()
        rows = [radar_row("a", "1", 100), radar_row("a", "2", 200), radar_row("a", "3", 300),
                radar_row("a", "4", 900_000), radar_row("a", "5", 50_000_000, pinned=True),
                radar_row("b", "6", 500_000), radar_row("c", "7", 700), radar_row("c", "8", 800), radar_row("c", "9", 2700)]
        ranked = {v["id"]: v for v in radar.rank_videos(rows, {"4": [(now - 7200, 800_000), (now, 900_000)]}, now)}
        # Mediana de «a» = 250: el fijado no cuenta, aunque sí se le calcula su múltiplo.
        self.assertEqual(ranked["4"]["multiple"], 3600.0)
        self.assertTrue(ranked["4"]["viral"])
        self.assertEqual(ranked["4"]["velocity"], 1_200_000)
        self.assertIsNone(ranked["1"]["velocity"])
        self.assertIsNone(ranked["6"]["multiple"])    # con un solo video no hay mediana
        self.assertFalse(ranked["6"]["viral"])
        self.assertEqual(ranked["9"]["multiple"], 3.4)  # pocas veces sobre su mediana y pocas vistas: no es viral
        self.assertFalse(ranked["9"]["viral"])
        big = radar.rank_videos([radar_row("d", str(i), 300_000) for i in range(1, 4)]
                                + [radar_row("d", "4", 2_000_000), radar_row("e", "5", 1_000), radar_row("e", "6", 1_000),
                                   radar_row("e", "7", 1_000), radar_row("e", "8", 200_000)], {}, now)
        verdicts = {v["id"]: v["viral"] for v in big}
        self.assertFalse(verdicts["4"])  # muchas vistas, pero solo 6,7× su mediana
        self.assertFalse(verdicts["8"])  # 200× su mediana, pero menos de 250K
        self.assertEqual(radar.rank_videos(rows, {}, now)[0]["id"], "5")  # los virales, primero
        stats = radar.account_stats([v for v in ranked.values() if v["handle"] == "a"])
        self.assertEqual((stats["window_videos"], stats["median_views"], stats["viral_videos"]), (5, 250, 2))

    def test_json_is_found_among_other_text(self):
        niches = [{"nicho": "Coches", "senales": [{"url": "https://a.example/[x]"}]}]
        answer = json.dumps(niches, ensure_ascii=False)
        # Así respondió Claude tras buscar en la web: el JSON y luego sus fuentes en Markdown.
        sources = "\n\nSources:\n- [TikTok 2026](https://a.example/x)\n- [Nichos](https://b.example/y)"
        for text in (answer, answer + sources, f"Según [1] y []:\n```json\n{answer}\n```" + sources):
            with self.subTest(text=text[:30]):
                self.assertEqual(radar._extract_json(text, "["), niches)
        self.assertEqual(radar._extract_json('Aquí va: {"tema": "x"} y fin {nota}', "{"), {"tema": "x"})
        for bad in ("", "No encontré nada.", "Mira [1] y [2]", '[{"nicho": "cortado"'):
            with self.subTest(bad=bad), self.assertRaises(radar.RadarError):
                radar._extract_json(bad, "[")

    def test_clean_ideas(self):
        items = [
            {"tema": "  ¿Por qué  bostezas? ", "fuentes": ["111111", "inventado", "111111"], "gancho": "Hola"},
            {"tema": "¡POR QUÉ BOSTEZAS!"}, {"tema": "Ya hecho"}, {"tema": "x" * 71}, {"tema": ""}, "no es un objeto",
            {"tema": "Sin fuentes", "fuentes": "111111"},
        ]
        ideas = radar.clean_ideas(items, {"111111"}, ["ya hecho"])
        self.assertEqual([i["tema"] for i in ideas], ["¿Por qué bostezas?", "Sin fuentes"])
        self.assertEqual((ideas[0]["fuentes"], ideas[0]["gancho"]), (["111111"], "Hola"))
        self.assertEqual(ideas[1]["fuentes"], [])
        self.assertEqual(len(radar.clean_ideas([{"tema": f"Tema {i}"} for i in range(9)], set(), [], 5)), 5)

    def test_radar_videos_count_as_proof(self):
        url = radar.video_url("importacrew", "7700576236283759894")
        answer = [{"nicho": "Coches", "puntaje": 9, "senales": [{"detalle": "5M de vistas", "url": url}]}]

        async def fake_claude(prompt, tools=None, max_turns=1):
            self.assertIn(url, prompt)
            self.assertEqual(tools, ["WebSearch", "WebFetch"])  # sin Bash ni herramientas que escriban
            return json.dumps(answer) + "\n\nSources:\n- [x](https://a.example)", {"https://a.example"}

        original, radar.ask_claude = radar.ask_claude, fake_claude
        try:
            items, found = asyncio.run(radar.find_niches("", [{"handle": "importacrew", "views": 5_000_000, "url": url, "text": "x"}]))
        finally:
            radar.ask_claude = original
        niche = radar.clean_niches(items, found)[0]
        self.assertEqual((niche["puntaje"], niche["senales"][0]["url"]), (9, url))

    def test_niches_only_keep_links_from_the_search(self):
        items = [
            {"nicho": "Con prueba", "puntaje": 9, "cuentas": ["@Real.Cuenta", "no vale!"],
             "senales": [{"detalle": "a", "url": "https://real.example/x/"}]},
            {"nicho": "Enlace inventado", "puntaje": 9, "senales": [{"detalle": "b", "url": "https://inventado.example"}]},
            {"nicho": "Enlace peligroso", "puntaje": 7, "senales": [{"detalle": "c", "url": "javascript:alert(1)"}]},
            {"nicho": "", "puntaje": 10}, "no es un objeto",
            {"nicho": "Puntaje raro", "puntaje": "mucho"},
        ]
        niches = {n["nicho"]: n for n in radar.clean_niches(items, {"https://real.example/x"})}
        self.assertEqual(niches["Con prueba"]["puntaje"], 9)
        self.assertEqual(niches["Con prueba"]["cuentas"], ["real.cuenta"])
        self.assertEqual(niches["Enlace inventado"]["puntaje"], 6)
        self.assertEqual(niches["Enlace inventado"]["senales"][0]["url"], "")
        self.assertEqual(niches["Enlace peligroso"]["senales"][0]["url"], "")
        self.assertEqual(niches["Puntaje raro"]["puntaje"], 1)
        self.assertEqual(len(niches), 4)

    def test_apify_source(self):
        calls = []

        def http(method, url, headers, body, timeout):
            calls.append((url, headers, json.loads(body)))
            return 200, json.dumps([
                {"id": "7705573604842360096", "text": "Hola", "createTime": 1800000000, "playCount": 1500, "diggCount": 10,
                 "commentCount": 2, "shareCount": 3, "collectCount": 4, "isPinned": True,
                 "authorMeta": {"name": "UnaCuenta", "fans": 99, "video": 7},
                 "videoMeta": {"duration": 65, "coverUrl": "https://cdn.example/c.jpg"}},
                {"error": "fila de error sin id", "authorMeta": {"name": "unacuenta"}},
                {"id": "7705573604842360097", "authorMeta": {"name": "otra-que-no-pedi"}},
            ]).encode()

        result = radar.apify_source("clave-secreta", http)(["unacuenta"], lambda _: None)
        self.assertNotIn("clave-secreta", calls[0][0])  # la clave no va en la URL
        self.assertEqual(calls[0][1]["Authorization"], "Bearer clave-secreta")
        self.assertFalse(calls[0][2]["shouldDownloadVideos"])  # lo que cobra aparte, apagado
        self.assertEqual((result["unacuenta"]["followers"], len(result["unacuenta"]["videos"])), (99, 1))
        video = result["unacuenta"]["videos"][0]
        self.assertEqual((video["views"], video["saves"], video["duration"], video["pinned"]), (1500, 4, 65, True))
        for status, text in ((401, "rechazó la clave"), (402, "crédito")):
            with self.subTest(status=status), self.assertRaises(radar.RadarError) as ctx:
                radar.apify_source("clave", lambda *a: (status, b""))(["x1"], lambda _: None)
            self.assertIn(text, str(ctx.exception))

    def test_apify_token_only_goes_to_apify(self):
        seen = {}

        def http(method, url, headers, body, timeout):
            if method == "POST":
                return 200, json.dumps([{"id": "123456", "mediaUrls": ["https://cdn.example/v.mp4"]}]).encode()
            seen.update(url=url, headers=headers)
            return 200, b"\x00\x00\x00\x18ftypmp42"

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "v.mp4"
            self.assertTrue(radar.apify_video_fetcher("clave", http)({"id": "123456", "url": "u"}, dest))
            self.assertTrue(dest.exists())
        self.assertNotIn("Authorization", seen["headers"])

    def test_authors_from_search(self):
        def item(video_id, name, views):
            return {"id": video_id, "playCount": views, "text": f"v{views}", "authorMeta": {"name": name, "fans": 5}}

        authors = radar.authors_from([item("100001", "Una", 10), item("100002", "una", 500), item("100003", "otra", 400),
                                      item("100004", "no vale!", 9999), {"error": "fila sin id"}])
        self.assertEqual([a["handle"] for a in authors], ["una", "otra"])
        self.assertEqual((authors[0]["videos"], authors[0]["views"], authors[0]["best_views"], authors[0]["best_text"]),
                         (2, 510, 500, "v500"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "hace falta ffmpeg")
    def test_video_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "v.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:r=24:d=10",
                            "-pix_fmt", "yuv420p", str(video)], check=True, capture_output=True)
            frames = radar.video_frames(video, Path(tmp))
        self.assertEqual(len(frames), 5)
        self.assertTrue(frames[0].startswith(b"\xff\xd8\xff"))


class RadarApiTests(unittest.TestCase):
    def client(self, data, **parts):
        manager = JobManager(TMP / data, runner=run_demo)
        parts = {"source": demo_radar_source, "analyzer": analyze_demo, "niche_finder": find_niches_demo,
                 "account_searcher": demo_account_search, "idea_maker": make_ideas_demo, **parts}
        box = radar.Radar(manager.store.db, manager.data_dir, **parts)
        return TestClient(create_app(manager, token="", radar=box), base_url="http://127.0.0.1")

    def wait(self, client, done, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = client.get("/api/radar").json()
            if done(state):
                return state
            time.sleep(0.02)
        raise AssertionError(f"no terminó: {state}")

    def test_scan_analyze_and_patterns(self):
        with self.client("data-radar") as client:
            state = client.get("/api/radar").json()
            self.assertEqual([a["handle"] for a in state["accounts"]], radar.DEFAULT_ACCOUNTS)
            self.assertEqual(state["videos"], [])

            self.assertEqual(client.post("/api/radar/accounts", json={"handle": "no vale"}).status_code, 400)
            res = client.post("/api/radar/accounts", json={"handle": "https://www.tiktok.com/@otra.cuenta"})
            self.assertIn("otra.cuenta", [a["handle"] for a in res.json()["accounts"]])
            client.post("/api/radar/accounts", json={"handle": "@otra.cuenta"})  # no se duplica
            self.assertEqual(client.delete("/api/radar/accounts/otra.cuenta").status_code, 200)

            client.post("/api/radar/scan", json={})
            state = self.wait(client, lambda s: not s["scanning"])
            self.assertEqual(state["scan"]["state"], "done", state["scan"])
            self.assertEqual(len(state["videos"]), 36)
            top = state["videos"][0]
            self.assertTrue(top["viral"] and top["pinned"] and top["url"].startswith("https://www.tiktok.com/@"))
            self.assertEqual(state["patterns"]["virales"], 3)

            self.assertEqual(client.post("/api/radar/videos/123456/analyze").status_code, 404)
            self.assertEqual(client.post("/api/radar/videos/..%2F..%2Fx/analyze").status_code, 404)
            client.post(f"/api/radar/videos/{top['id']}/analyze")
            state = self.wait(client, lambda s: not s["videos"][0]["analyzing"] and s["videos"][0]["analysis"])
            analysis = state["videos"][0]["analysis"]
            self.assertTrue(analysis["parcial"])  # sin video descargado, lo dice
            self.assertEqual(state["patterns"]["ganchos"][0]["valor"], "afirmación que parece falsa")

            # Quitar una cuenta borra lo suyo.
            client.delete(f"/api/radar/accounts/{top['handle']}")
            state = client.get("/api/radar").json()
            self.assertEqual(len(state["videos"]), 24)
            self.assertEqual(state["patterns"]["analizados"], 0)

    def test_failures_are_readable_and_per_account(self):
        def source(handles, progress):
            return {"importacrew": {"error": "TikTok pidió verificar."},
                    "ingenioculto": demo_radar_source(["ingenioculto"], progress)["ingenioculto"]}

        with self.client("data-radar-fail", source=source) as client:
            client.post("/api/radar/scan", json={})
            state = self.wait(client, lambda s: not s["scanning"])
            errors = {a["handle"]: a["last_error"] for a in state["accounts"]}
            self.assertEqual(errors["importacrew"], "TikTok pidió verificar.")
            self.assertIn("No devolvió videos", errors["nuncateloexplicaron"])
            self.assertIsNone(errors["ingenioculto"])
            self.assertEqual(state["scan"]["state"], "done")
            self.assertIn("2 cuenta(s) con error", state["scan"]["step"])

        def broken(handles, progress):
            raise radar.RadarError("Apify rechazó la clave.")

        with self.client("data-radar-broken", source=broken) as client:
            client.post("/api/radar/scan", json={})
            state = self.wait(client, lambda s: not s["scanning"])
            self.assertEqual(state["scan"], {"state": "failed", "step": "Apify rechazó la clave."})

    def test_niches(self):
        with self.client("data-radar-niches") as client:
            client.post("/api/radar/niches", json={"tema": "coches"})
            self.assertEqual(client.post("/api/radar/niches", json={}).status_code, 409)  # una a la vez
            state = self.wait(client, lambda s: s["niche_runs"][0]["status"] != "running")
            run = state["niche_runs"][0]
            self.assertEqual((run["status"], run["tema"]), ("done", "coches"))
            scores = {n["nicho"]: n["puntaje"] for n in run["nichos"]}
            self.assertEqual(scores["La ciencia de lo que comes cada día"], 6)  # sin enlace, baja
            self.assertEqual(run["nichos"][0]["puntaje"], 9)
            # Las cuentas reales de un nicho, solo si se piden.
            self.assertNotIn("encontradas", run["nichos"][0])
            self.assertEqual(client.post(f"/api/radar/niches/{run['id']}/99/accounts").status_code, 404)
            self.assertEqual(client.post("/api/radar/niches/nada/0/accounts").status_code, 404)
            self.assertEqual(client.post(f"/api/radar/niches/{run['id']}/0/accounts").status_code, 200)
            state = self.wait(client, lambda s: not s["searching"])
            found = state["niche_runs"][0]["nichos"][0]
            self.assertEqual(found["encontradas"][0]["handle"], "motor.curioso")
            self.assertIsNone(found["busqueda_error"])

    def test_ideas_come_from_the_chosen_niche(self):
        with self.client("data-radar-ideas", used_topics=lambda idioma: ["¿Por qué bostezas cuando otro bosteza?"]) as client:
            self.assertEqual(client.post("/api/radar/ideas").status_code, 409)  # sin nicho elegido
            client.post("/api/radar/scan", json={})
            self.wait(client, lambda s: not s["scanning"])
            client.post("/api/radar/niches", json={})
            run = self.wait(client, lambda s: s["niche_runs"][0]["status"] != "running")["niche_runs"][0]
            self.assertEqual(client.post(f"/api/radar/niches/{run['id']}/99/choose").status_code, 404)
            state = client.post(f"/api/radar/niches/{run['id']}/1/choose").json()
            self.assertEqual(state["active_niche"]["nicho"], run["nichos"][1]["nicho"])

            client.post("/api/radar/ideas")
            state = self.wait(client, lambda s: not s["making_ideas"])
            self.assertIsNone(state["ideas_error"])
            temas = [i["tema"] for i in state["ideas"]]
            self.assertEqual(len(temas), 5)
            self.assertNotIn("¿Por qué bostezas cuando otro bosteza?", temas)  # ya hecho
            self.assertTrue(state["ideas"][0]["fuentes"][0]["url"].startswith("https://www.tiktok.com/@"))
            self.assertFalse(state["ideas"][0]["usada"])

            discarded = state["ideas"][0]
            self.assertEqual(len(client.delete(f"/api/radar/ideas/{discarded['id']}").json()["ideas"]), 4)
            self.assertEqual(client.delete("/api/radar/ideas/nada").status_code, 404)
            client.post("/api/radar/ideas")
            state = self.wait(client, lambda s: not s["making_ideas"])
            # Ni lo mostrado ni lo descartado se repite: el demo solo tiene 2 temas más.
            self.assertEqual(len(state["ideas"]), 6)
            self.assertNotIn(discarded["tema"], [i["tema"] for i in state["ideas"]])
            client.post("/api/radar/ideas")
            state = self.wait(client, lambda s: not s["making_ideas"])
            self.assertIn("No salieron ideas nuevas", state["ideas_error"])

    def test_account_search_needs_a_source(self):
        manager = JobManager(TMP / "data-radar-nokey", runner=run_demo)
        box = radar.Radar(manager.store.db, manager.data_dir, niche_finder=find_niches_demo)
        with TestClient(create_app(manager, token="", radar=box), base_url="http://127.0.0.1") as client:
            self.assertFalse(client.get("/api/radar").json()["config"]["can_search"])
            client.post("/api/radar/niches", json={})
            run = self.wait(client, lambda s: s["niche_runs"][0]["status"] != "running")["niche_runs"][0]
            res = client.post(f"/api/radar/niches/{run['id']}/0/accounts")
            self.assertEqual(res.status_code, 409)
            self.assertIn("clave de Apify", res.json()["error"])

    def test_apify_key_never_reaches_the_browser(self):
        manager = JobManager(TMP / "data-radar-key", runner=run_demo)
        box = radar.Radar(manager.store.db, manager.data_dir)
        with TestClient(create_app(manager, token="", radar=box), base_url="http://127.0.0.1") as client:
            self.assertEqual(client.get("/api/radar").json()["config"]["source"], "sesion")
            self.assertEqual(client.put("/api/radar/config", json={"apify_token": "corta"}).status_code, 409)
            res = client.put("/api/radar/config", json={"apify_token": "apify_api_secreta1234"})
            self.assertNotIn("apify_api_secreta", res.text)
            self.assertEqual(res.json()["config"]["apify_hint"], "…1234")
            self.assertEqual(res.json()["config"]["source"], "apify")
        os.environ["PAPER_STAGE_APIFY_TOKEN"] = "secreta"
        try:
            self.assertEqual(core.agent_env()["PAPER_STAGE_APIFY_TOKEN"], "")
        finally:
            del os.environ["PAPER_STAGE_APIFY_TOKEN"]

    def test_cover_route_only_serves_covers(self):
        with self.client("data-radar-cover") as client:
            box = client.app.state.radar
            (box.covers_dir / "123456.jpg").write_bytes(b"\xff\xd8\xff")
            self.assertEqual(client.get("/radar/cover/123456.jpg").status_code, 200)
            self.assertEqual(client.get("/radar/cover/999999.jpg").status_code, 404)
            self.assertEqual(client.get("/radar/cover/..%2F..%2Fpaper_stage.db.jpg").status_code, 404)

    def test_cover_download_rejects_non_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.jpg"
            radar._save_cover(path, "http://cdn.example/c.jpg", lambda *a: (200, b"\xff\xd8\xff"))  # sin https
            radar._save_cover(path, "https://cdn.example/c.jpg", lambda *a: (200, b"<html>"))
            self.assertFalse(path.exists())
            radar._save_cover(path, "https://cdn.example/c.jpg", lambda *a: (200, b"\xff\xd8\xffdatos"))
            self.assertTrue(path.exists())


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_waits_for_cancellation_in_progress(self):
        closed = []

        async def runner(req, *, resume_session=None, continuing=False, on_stderr=None):
            yield {"kind": "session", "session_id": "s"}
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.sleep(0.5)  # como el SDK cerrando el proceso de Claude Code
                closed.append(True)

        manager = JobManager(TMP / "data-shutdown", runner=runner)
        await manager.start()
        job = manager.create(core.VideoRequest(tema="Prueba de apagado"))
        await asyncio.sleep(0.2)
        manager.cancel(job["id"])
        await asyncio.wait_for(manager.stop(), timeout=10)
        self.assertEqual(closed, [True])
        self.assertEqual(manager.get(job["id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
