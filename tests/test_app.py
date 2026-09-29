"""Pruebas de la app: python -m unittest discover tests

Usan el agente de demostración (sin Claude) y directorios temporales.
"""

import asyncio
import json
import os
import shutil
import sqlite3
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
from paper_stage.demo import run_demo  # noqa: E402
from paper_stage.jobs import JobManager  # noqa: E402
from paper_stage import publishing  # noqa: E402

# Las pruebas nunca deben escribir en el Google Drive real de quien las ejecuta.
publishing.drive_roots = lambda: []
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
    "nombre": "Coral", "nicho": "Animales del océano profundo", "idioma": "en", "edad": "4-6",
    "apariencia": "Una pulpita lila de papel con casco de buzo y una linterna amarilla en un tentáculo.",
    "personalidad": "Valiente y curiosa, habla en susurros emocionados.",
    "escenario": "Un submarino de papel con ojo de buey redondo.",
    "paleta": ["#2b4c7e", "#6FB7B7", "#B58BD6", "#12213D"],
    "voces": {"es": "ef_dora", "en": "af_sky"}, "voz_estilo": "juvenil y susurrada",
    "serie": "Coral Under the Sea", "cta": {"en": "Follow me to dive deeper!"},
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
        for bad in (dict(tema="x"), dict(tema="Volcanes", edad="seis"),
                    dict(tema="Volcanes", idioma="fr"), dict(tema="Volcanes", max_budget_usd=0.1)):
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

    def test_default_character_is_lia(self):
        req = core.VideoRequest(tema="Volcanes")
        req.validate()
        self.assertEqual((req.idioma, req.edad, req.character.id), ("es", "6-9", "lia"))
        prompt = core.render_prompt(req.variables())
        self.assertIn("presentado por **Lía**", prompt)
        self.assertIn("Kokoro `ef_dora` (lang_code `e`)", prompt)
        self.assertIn('"serie": "Teatrito de Papel"', prompt)
        self.assertIn("#teatritodepapel", prompt)
        self.assertIn("primer video de Lía", prompt)
        self.assertIn("gsap.timeline({ paused: true })", prompt)
        self.assertIn("`renderAt` **no devuelve nada**", prompt)

    def test_custom_character_prompt(self):
        character = core.Character(**CORAL)
        character.id = "coral"
        character.validate()
        req = core.VideoRequest(tema="Why do anglerfish glow?", personaje=character.to_dict(),
                                referencias=["old-coral-video-en"])
        req.validate()
        # Toma el idioma, la edad y la frase final del personaje.
        self.assertEqual((req.idioma, req.edad, req.cta), ("en", "4-6", "Follow me to dive deeper!"))
        prompt = core.render_prompt(req.variables())
        self.assertNotIn("Lía", prompt)
        self.assertNotIn("{{", prompt)
        for text in ("presentado por **Coral**", "Animales del océano profundo", "Kokoro `af_sky` (lang_code `a`)",
                     "`#2B4C7E`", "#coralunderthesea", '"personaje": "coral"', "`output/old-coral-video-en/`",
                     "Un submarino de papel"):
            self.assertIn(text, prompt)
        self.assertIn("Produce Coral's full video", core.kickoff_prompt(req))

    def test_script_character(self):
        self.assertEqual(library.script_character({}), "lia")               # anterior a los personajes
        self.assertEqual(library.script_character({"personaje": "Lía"}), "lia")  # nombre en vez de ID
        self.assertEqual(library.script_character({"personaje": "coral-2"}), "coral-2")

    def test_character_validation(self):
        for bad in (dict(paleta=["#FFF", "#000000", "#123456"]), dict(paleta=["#000000"] * 2),
                    dict(voces={"es": "af_heart", "en": "af_sky"}), dict(nicho="x"),
                    dict(idioma="fr"), dict(cta={"es": "x" * 81})):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                core.Character(**{**CORAL, **bad}).validate()

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
        return TestClient(create_app(manager, token=token or ""))

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
            self.assertEqual(len(detail["json"]["script.json"]["escenas"]), 7)
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
                res = client.post("/api/suggest", json={"idioma": "es", "edad": "6-9", "evitar": shown})
                self.assertEqual(res.status_code, 200, res.text)
                temas = [t["tema"] for t in res.json()["temas"]]
                self.assertEqual(len(temas), 3)
                shown += temas
            keys = [core.topic_key(t, "es") for t in shown]
            self.assertEqual(len(set(keys)), 6)
            self.assertNotIn(core.topic_key("¿Por qué el mar es salado?", "es"), keys)

    def test_characters(self):
        with self.make_client(data="data-characters") as client:
            chars = client.get("/api/characters").json()
            self.assertEqual([c["id"] for c in chars], ["lia"])  # siempre hay al menos a Lía

            ideas = client.post("/api/characters/suggest", json={"idioma": "es", "edad": "6-9"}).json()["personajes"]
            self.assertEqual(len(ideas), 3)
            again = client.post("/api/characters/suggest", json={
                "idioma": "es", "edad": "6-9",
                "evitar": [f"{i['personaje']['nombre']} ({i['personaje']['nicho']})" for i in ideas]})
            names = {i["personaje"]["nombre"] for i in ideas}
            self.assertFalse(names & {i["personaje"]["nombre"] for i in again.json().get("personajes", [])})

            self.assertEqual(client.post("/api/characters", json={**CORAL, "paleta": ["#123"]}).status_code, 400)
            self.assertEqual(client.post("/api/characters", json={"nombre": "Solo nombre"}).status_code, 400)
            res = client.post("/api/characters", json=CORAL)
            self.assertEqual(res.status_code, 201, res.text)
            coral = res.json()
            self.assertEqual((coral["id"], coral["hashtag"]), ("coral", "#coralunderthesea"))
            self.assertEqual(client.post("/api/characters", json=CORAL).json()["id"], "coral-2")

            res = client.post("/api/prompt-preview", json={"tema": "Anglerfish", "personaje_id": "coral"})
            self.assertIn("presentado por **Coral**", res.json()["prompt"])
            self.assertEqual(res.json()["slug"], "anglerfish-en")
            self.assertEqual(client.post("/api/prompt-preview", json={"tema": "X y", "personaje_id": "nadie"}).status_code, 400)

            topics = client.post("/api/suggest", json={"personaje_id": "coral"})
            self.assertEqual(topics.status_code, 200, topics.text)

            # El video lleva el personaje y su idioma; el mismo tema no se repite en su serie,
            # pero otro personaje sí puede tratarlo.
            job = client.post("/api/jobs", json={"tema": "Las medusas", "personaje_id": "coral"}).json()
            self.assertEqual(job["request"]["personaje"]["nombre"], "Coral")
            self.assertEqual(client.delete("/api/characters/coral").status_code, 409)  # en producción
            job = wait_for(client, job["id"], {"done", "failed"})
            self.assertEqual(job["status"], "done", job["error"])
            video = client.get(f"/api/videos/{job['slug']}").json()
            self.assertEqual((video["personaje"], video["idioma"]), ("coral", "en"))
            self.assertIn("Coral", video["texts"]["bible.md"])
            self.assertEqual(client.post("/api/jobs", json={"tema": "las medusas", "personaje_id": "coral"}).status_code, 400)
            lia = client.post("/api/jobs", json={"tema": "Las medusas", "idioma": "en"})
            self.assertEqual(lia.status_code, 201, lia.text)
            wait_for(client, lia.json()["id"], {"done", "failed"})

            # Su segundo video copia el diseño del primero y ve su gancho, para no repetir la apertura.
            res = client.post("/api/prompt-preview", json={"tema": "Octopus ink", "personaje_id": "coral"})
            self.assertIn(f"`output/{job['slug']}/`", res.json()["prompt"])
            self.assertIn("- pregunta imposible: Did you know that Las medusas", res.json()["prompt"])
            self.assertIn("- (ninguno todavía)", client.post("/api/prompt-preview", json={
                "tema": "Otro tema", "personaje_id": "coral-2"}).json()["prompt"])

            listed = {c["id"]: c for c in client.get("/api/characters").json()}
            self.assertEqual((listed["coral"]["videos"], listed["coral"]["ultimo_video"]), (1, job["slug"]))
            res = client.put("/api/characters/coral", json={**CORAL, "nicho": "Criaturas del arrecife"})
            self.assertEqual(res.json()["nicho"], "Criaturas del arrecife")
            self.assertEqual(client.put("/api/characters/nadie", json=CORAL).status_code, 404)
            self.assertEqual(client.delete("/api/characters/coral-2").status_code, 200)
            self.assertEqual(client.get("/api/characters/coral-2").status_code, 404)

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
            channels = client.get("/api/publishing/channels").json()
            cfg = client.put("/api/publishing", json={"channel_id": channels[0]["id"], "channel_name": channels[0]["nombre"]}).json()
            self.assertTrue(cfg["ready"])
            info = client.get(f"/api/videos/{job['slug']}/publish").json()
            self.assertIn("#", info["default_text"])
            res = client.post(f"/api/videos/{job['slug']}/publish", json={"text": info["default_text"], "mode": "queue"})
            self.assertEqual(res.status_code, 200, res.text)
            deadline = time.time() + 20
            while time.time() < deadline:
                state = client.get(f"/api/videos/{job['slug']}/publish").json()
                if state["status"]["state"] != "running":
                    break
                time.sleep(0.1)
            self.assertEqual(state["status"]["state"], "done", state)
            self.assertEqual(len(state["history"]), 1)
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
            self.assertIn("full video about “El arcoíris”", res.json()["kickoff"])
            self.assertEqual(client.post("/api/jobs", json={"tema": ""}).status_code, 400)
            self.assertEqual(client.post("/api/jobs", content=b"no-json").status_code, 400)


class FakeNet:
    """Buffer y la descarga de Drive simulados; guarda lo que se envió a Buffer."""

    def __init__(self, public=True, key_ok=True, login=False):
        self.public, self.key_ok, self.login, self.posts = public, key_ok, login, []

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
            data = {"createPost": {"post": {"id": f"p{len(self.posts)}", "dueAt": "2026-10-01T15:00:00Z"}}}
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
                         "create_folder": True, "channel_id": "tt1", "channel_name": "@teatrito"})
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
            self.assertTrue((self.folder / "prueba-teatrito.mp4").exists())

    def test_agent_does_not_see_secrets(self):
        os.environ["PAPER_STAGE_BUFFER_KEY"] = "secreta"
        try:
            self.assertEqual(core.agent_env()["PAPER_STAGE_BUFFER_KEY"], "")
        finally:
            del os.environ["PAPER_STAGE_BUFFER_KEY"]


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
