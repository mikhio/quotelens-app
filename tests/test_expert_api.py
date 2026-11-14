from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from quotelens import server
from quotelens.storage import Store
from tests.test_review_acceptances import fixture, JOB


class MeteredResearch:
    def __init__(self, store, job_id, payload, cancel, event):
        self.store, self.id, self.payload = store, job_id, payload

    def run(self):
        result, _, _ = fixture()
        usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": .002}
        self.store.add_step(self.id, {"kind": "model", "usage": usage})
        result["search"]["budget"].update(model_calls=1, pages_used=1, actual_usage=usage, actual_cost_known=True)
        self.store.save_result(self.id, result)
        return result


class ExpertApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "quotelens.sqlite3")
        for p in [patch.object(server, "DATA_DIR", Path(self.tmp.name)), patch.object(server, "_STORE", self.store),
                  patch.object(server, "JOBS", {}), patch.object(server, "ACTIVE", threading.BoundedSemaphore(2)),
                  patch("quotelens.procurement.ProcurementResearch", MeteredResearch)]:
            p.start(); self.addCleanup(p.stop)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:" + str(self.http.server_address[1])
        self.addCleanup(self.http.server_close); self.addCleanup(self.http.shutdown)

    def request(self, path, payload=None):
        req = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json"} if payload is not None else {})
        try: response = urlopen(req, timeout=30)
        except HTTPError as exc: response = exc
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw

    def wait(self, identifier):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            _, job = self.request("/api/jobs/" + identifier)
            if job["status"] != "running": return job
            time.sleep(.02)
        self.fail("Synthetic worker timeout")

    def test_search_continue_cost_and_share_work_through_real_http(self):
        _, payload, _ = fixture()
        code, first = self.request("/api/search", payload)
        self.assertEqual(code, 202)
        identifier = first["id"]
        job = self.wait(identifier)
        self.assertEqual(job["status"], "complete", job.get("error"))
        self.assertEqual(job["result"]["search"]["budget"]["actual_usage"]["total_tokens"], 10)
        self.assertEqual(len(job["result"]["search"]["passes"]), 1)
        code, _ = self.request("/api/jobs/" + identifier + "/continue", {"max_seconds": 20, "expected_revision": 0})
        self.assertEqual(code, 202)
        job = self.wait(identifier)
        self.assertEqual(job["revision"], 1)
        search = job["result"]["search"]
        self.assertEqual(len(search["passes"]), 2)
        self.assertEqual(search["budget"]["model_calls"], 2)
        self.assertEqual(search["budget"]["actual_usage"]["total_tokens"], 20)
        self.assertEqual(search["budget"]["actual_usage"]["cost"], .004)
        self.assertEqual(self.request("/api/jobs/" + identifier + "/continue", {"max_seconds": 20, "expected_revision": 0})[0], 409)
        code, costs = self.request("/api/costs")
        self.assertEqual(code, 200)
        self.assertEqual(costs["totals"]["model_calls"], 2)
        self.assertEqual(costs["totals"]["total_tokens"], 20)
        self.assertEqual(costs["totals"]["cost_usd"], .004)
        self.assertTrue(costs["totals"]["cost_known"])
        code, share = self.request("/api/jobs/" + identifier + "/share", {"expected_revision": 1})
        self.assertEqual(code, 201, share)
        code, page = self.request(share["url"])
        self.assertEqual(code, 200)
        self.assertNotIn(identifier.encode(), page)
        self.assertNotIn(b"actual_usage", page)
        self.assertNotIn(b"/api/jobs", page)
        self.assertEqual(self.request(share["url"], {})[0], 405)
        self.assertEqual(self.request(share["url"] + "../jobs")[0], 404)

    def test_missing_receipt_cost_is_unknown_and_discovery_is_included_once(self):
        result, payload, _ = fixture()
        result["search"]["budget"].update(model_calls=2)
        self.store.create_job({"id": JOB, "status": "complete", "result": result}, payload)
        self.store.add_step(JOB, {"kind": "model", "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost": .01}})
        self.store.add_step(JOB, {"kind": "discovery", "output": {"usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23}}})
        report = self.store.cost_report()
        self.assertFalse(report["totals"]["cost_known"])
        self.assertFalse(report["jobs"][0]["cost_known"])
        self.assertIsNone(report["jobs"][0]["cost_usd"])
        self.assertEqual(report["jobs"][0]["total_tokens"], 35)
        self.assertEqual(report["jobs"][0]["known_cost_usd"], .01)


if __name__ == "__main__": unittest.main()
