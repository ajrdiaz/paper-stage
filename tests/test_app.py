"""Pruebas de la app: python -m unittest discover tests

Usan el agente de demostración (sin Claude) y directorios temporales.
"""

import asyncio
import json
import os
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

from paper_stage import core  # noqa: E402
from paper_stage.demo import run_demo  # noqa: E402
from paper_stage.jobs import JobManager  # noqa: E402
from paper_stage.web import create_app  # noqa: E402


async def slow_runner(req, *, resume_session=None, continuing=False, on_stderr=None):
    yield {"kind": "session", "session_id": "slow-session"}
    if continuing:
        async for event in run_demo(req, resume_session=resume_session, continuing=True):
            yield event
        return
    on_stderr and on_stderr("arrancando CLI")
    await asyncio.sleep(30)


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

            # El mismo tema otra vez recibe otra carpeta.
            again = client.post("/api/jobs", json={"tema": "Los delfines", "idioma": "es"}).json()
            self.assertEqual(again["slug"], "los-delfines-es-2")
            wait_for(client, again["id"], {"done", "failed"})

            videos = {v["slug"]: v for v in client.get("/api/videos").json()}
            self.assertIn("los-delfines-es", videos)
            detail = client.get("/api/videos/los-delfines-es").json()
            self.assertEqual(len(detail["json"]["script.json"]["escenas"]), 7)
            self.assertEqual(detail["jobs"][0]["id"], job["id"])
            self.assertEqual(client.get("/files/los-delfines-es/stage.html").status_code, 200)
            self.assertEqual(client.get("/files/los-delfines-es/../../data/paper_stage.db").status_code, 404)
            self.assertEqual(client.get("/files/los-delfines-es/agent_log.jsonl").status_code, 404)
            self.assertEqual(client.get("/api/videos/..%2Fdata").status_code, 404)

            events = client.get(f"/api/jobs/{job['id']}/events").text
            self.assertIn('"kind": "tool"', events)
            self.assertTrue(events.rstrip().endswith("data: {}"))

            self.assertEqual(client.delete("/api/videos/los-delfines-es-2").status_code, 200)
            self.assertFalse((core.OUTPUT_DIR / "los-delfines-es-2").exists())

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

    def test_prompt_preview_and_errors(self):
        with self.make_client(data="data-preview") as client:
            res = client.post("/api/prompt-preview", json={"tema": "El arcoíris", "idioma": "en"})
            self.assertEqual(res.json()["slug"], "el-arcoiris-en")
            self.assertIn("Produce the full video", res.json()["kickoff"])
            self.assertEqual(client.post("/api/jobs", json={"tema": ""}).status_code, 400)
            self.assertEqual(client.post("/api/jobs", content=b"no-json").status_code, 400)


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
