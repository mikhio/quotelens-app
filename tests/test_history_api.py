"""Real HTTP/SQLite acceptance for background searches, without paid searches.

The production Handler, worker lifecycle, quota accounting and Store run intact.
Only Research is replaced by a gated, explicitly synthetic test computation.
Run: TMPDIR=/managed/run python3 -m unittest discover -s tests -p test_history_api.py -v
"""
from __future__ import annotations

import copy
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from quotelens import server
from quotelens.engine import analyze_offers, parse_request
from quotelens.scenarios import load_scenario
from quotelens.storage import Store


DESCRIPTION = "QA: осциллограф цифровой, 4 канала, полоса не менее 100 МГц"


def synthetic_result(payload):
    """Exercise the real price engine with visibly labelled, invented offers."""
    offers = copy.deepcopy(load_scenario("synthetic-comparable")["offers"])
    assert offers and all(offer.get("provenance") == "synthetic" for offer in offers)
    request = parse_request(payload["description"], payload.get("quantity", 1))
    result = analyze_offers(request, offers, {"min_suppliers": 2})
    result["search"] = {
        "mode": "live_agent", "scenario_kind": "synthetic",
        "started_at": "2026-11-11T08:00:00Z", "finished_at": "2026-11-11T08:00:01Z",
        "min_suppliers": 2, "source_statuses": [], "model_used": False,
        "completeness": "bounded_not_exhaustive",
        "stop": {"code": "qa_complete", "label": "Учебный поиск завершён",
                 "detail": "QA: цены и поставщики вымышлены; сеть и модель не использовались."},
        "budget": {"pages_used": 0, "queries_used": 0, "model_calls": 0,
                   "elapsed_seconds": 0, "max_pages": 1, "max_queries": 1,
                   "max_model_calls": 0, "max_seconds": 30},
    }
    return result


class HistoryServerFixture:
    """One real application server with a unique SQLite and bounded workers.

    Test-only HTTP fault injection occurs before delegating to the real routes;
    successful requests always use the production Handler and Store.
    """

    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="quotelens-history-", dir=os.environ.get("TMPDIR"))
        self.directory = Path(self.temp.name)
        self.db = Store(self.directory / "quotelens.sqlite3")
        self.calls = []
        self.controls = {}
        self.fail_descriptions = set()
        self.faults = {}
        self.failed_paths = set()
        self.delayed_gets = {}
        self.active_delays = []
        self.hold_search_response = False
        self.search_response_ready = threading.Event()
        self.send_search_response = threading.Event()
        self.accepted_durable = []
        self.hold_next_history_response = False
        self.history_response_ready = threading.Event()
        self.send_history_response = threading.Event()
        self.history_response_snapshot = None
        self.mutex = threading.RLock()
        fixture = self

        class ControlledResearch:
            def __init__(self, store, identifier, payload, cancelled, event):
                self.identifier, self.payload = identifier, payload
                self.cancelled, self.event = cancelled, event
                with fixture.mutex:
                    fixture.controls[identifier] = {
                        "release": threading.Event(), "started": threading.Event(),
                        "cancelled": cancelled, "payload": copy.deepcopy(payload),
                    }

            def run(self):
                control = fixture.controls[self.identifier]
                self.event("QA: управляемый поиск, без сети и платных запросов.")
                control["started"].set()
                if not control["release"].wait(30):
                    raise RuntimeError("QA controller did not release the background worker")
                if self.payload["description"] in fixture.fail_descriptions:
                    raise RuntimeError("QA: deterministic research failure")
                return synthetic_result(self.payload)

        class ObservedHandler(server.Handler):
            def _observe(self):
                path = urlsplit(self.path).path
                with fixture.mutex:
                    fixture.calls.append({"method": self.command, "path": path})
                return path

            def do_GET(self):
                path = self._observe()
                with fixture.mutex:
                    remaining = fixture.faults.get(path, 0)
                    unavailable = path in fixture.failed_paths
                    if remaining:
                        fixture.faults[path] = remaining - 1
                    delay = fixture.delayed_gets.pop(path, None)
                    if delay:
                        fixture.active_delays.append(delay)
                if unavailable or remaining:
                    return self.respond(503, {"error": "QA: временная ошибка чтения. Повторите попытку."})
                if delay:
                    delay[0].set()
                    if not delay[1].wait(15):
                        return self.respond(503, {"error": "QA: delayed response expired"})
                return super().do_GET()

            def do_POST(self):
                self._observe()
                return super().do_POST()

            def respond(self, status, body, content_type="application/json; charset=utf-8", extra=None):
                if self.command == "GET" and status == 200 and urlsplit(self.path).path == "/api/jobs":
                    with fixture.mutex:
                        hold_history = fixture.hold_next_history_response
                        fixture.hold_next_history_response = False
                        if hold_history:
                            fixture.history_response_snapshot = copy.deepcopy(body)
                    if hold_history:
                        # The real route has already obtained its SQLite snapshot.
                        # Hold that old body, rather than delaying the Store read.
                        fixture.history_response_ready.set()
                        fixture.send_history_response.wait(15)
                if status == 202 and urlsplit(self.path).path == "/api/search":
                    # Reopen SQLite before a byte of 202 can reach the client.
                    durable = Store(fixture.db.path).get_job(body["id"])
                    with fixture.mutex:
                        fixture.accepted_durable.append(durable)
                    fixture.search_response_ready.set()
                    if fixture.hold_search_response:
                        fixture.send_search_response.wait(15)
                return super().respond(status, body, content_type, extra)

        self.handler = ObservedHandler
        self.patches = [
            patch.object(server, "DATA_DIR", self.directory),
            patch.object(server, "_STORE", self.db),
            patch.object(server, "JOBS", {}),
            patch.object(server, "ACTIVE", threading.BoundedSemaphore(2)),
            patch.object(server, "Research", ControlledResearch),
            patch.dict(os.environ, {"APP_SEARCH_RUNS_DAY": "100"}),
            patch.object(server.ModelProvider, "json", side_effect=AssertionError("Paid model call forbidden in QA")),
        ]
        for item in self.patches:
            item.start()
        self.start_http()

    def start_http(self):
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def stop_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def reopen(self):
        self.stop_http()
        self.db = Store(self.db.path)
        server._STORE = self.db
        server.JOBS.clear()
        self.start_http()

    def close(self):
        self.send_search_response.set()
        self.send_history_response.set()
        for control in list(self.controls.values()):
            control["release"].set()
        for _, release in list(self.delayed_gets.values()) + self.active_delays:
            release.set()
        try:
            self.wait(lambda: all(self.db.get_job(identifier)["status"] != "running"
                                 for identifier in self.controls), timeout=5)
        finally:
            self.stop_http()
            for item in reversed(self.patches):
                item.stop()
            self.temp.cleanup()

    @staticmethod
    def wait(predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        raise AssertionError("QA condition did not become true within %.1fs" % timeout)

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = Request(self.base + path, data=data,
                          headers={"Content-Type": "application/json"} if data else {})
        try:
            response = urlopen(request, timeout=8)
        except HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read()), dict(response.headers)

    def submit(self, description=DESCRIPTION, quantity=7, **extra):
        payload = {"description": description, "quantity": quantity,
                   "mode": "live", "source_ids": ["web"],
                   "limits": {"max_model_calls": 0}, **extra}
        status, body, _ = self.request("/api/search", payload)
        if status != 202:
            raise AssertionError((status, body))
        self.wait(lambda: body["id"] in self.controls)
        if not self.controls[body["id"]]["started"].wait(3):
            raise AssertionError("Real server worker did not start controlled Research")
        return body

    def finish(self, identifier):
        self.controls[identifier]["release"].set()
        return self.wait(lambda: (job if (job := self.db.get_job(identifier))["status"] != "running" else None))

    def count(self, method, path):
        with self.mutex:
            return sum(call == {"method": method, "path": path} for call in self.calls)

    def seed(self, number=1, status="complete", *, description=None, mode="live", created_at=None):
        identifier = format(number, "024x")
        payload = {"description": description or f"QA: учебная закупка {number}",
                   "quantity": number + 0.5, "source_ids": ["web"], "mode": mode}
        job = {"id": identifier, "status": status, "events": [],
               "created_at": created_at or "2026-11-11T08:00:00Z"}
        if status == "complete":
            job["result"] = synthetic_result(payload)
        if status in {"failed", "interrupted"}:
            job["error"] = "QA: учебная ошибка, данные сохранены."
        self.db.create_job(job, payload)
        self.db.add_event(identifier, {"time": job["created_at"], "kind": "info", "message": "QA: сохранённое событие"})
        return identifier


class HistoryApiAcceptance(unittest.TestCase):
    def setUp(self):
        self.fixture = HistoryServerFixture()
        self.addCleanup(self.fixture.close)

    def test_accepted_job_is_durable_and_restores_original_running_form_without_new_charge(self):
        fixture = self.fixture
        started = fixture.submit(description="  " + DESCRIPTION + "  ", quantity=3.25)
        expected = {"description": DESCRIPTION, "quantity": 3.25, "source_ids": ["web"]}
        self.assertEqual(started["request"], expected)
        self.assertTrue(started["created_at"])
        self.assertEqual(fixture.accepted_durable[0]["request"], expected)
        self.assertEqual(fixture.accepted_durable[0]["status"], "running")
        before = fixture.db.quota_usage("search_runs")
        self.assertEqual(before, 1)
        for _ in range(3):
            status, job, _ = fixture.request("/api/jobs/" + started["id"])
            self.assertEqual(status, 200)
            self.assertEqual(job["status"], "running")
            self.assertEqual(job["request"], expected)
            status, history, _ = fixture.request("/api/jobs")
            self.assertEqual(status, 200)
            self.assertEqual(history["total"], 1)
            self.assertEqual(history["items"][0]["request"], expected)
        self.assertEqual(fixture.db.quota_usage("search_runs"), before)
        self.assertEqual(fixture.count("POST", "/api/search"), 1)
        self.assertFalse(fixture.controls[started["id"]]["cancelled"].is_set())

    def test_lost_202_and_closed_client_finish_then_reopen_from_sqlite(self):
        fixture = self.fixture
        fixture.hold_search_response = True
        body = json.dumps({"description": DESCRIPTION, "quantity": 9, "mode": "live",
                           "source_ids": ["web"], "limits": {"max_model_calls": 0}}).encode()
        connection = socket.create_connection(("127.0.0.1", fixture.http.server_port), timeout=3)
        connection.sendall(("POST /api/search HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                            "Content-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body)).encode() + body)
        self.assertTrue(fixture.search_response_ready.wait(3))
        identifier = fixture.accepted_durable[0]["id"]
        connection.close()  # No response/id/hash has ever reached this client.
        fixture.send_search_response.set()
        fixture.wait(lambda: identifier in fixture.controls)
        self.assertFalse(fixture.controls[identifier]["cancelled"].is_set())
        completed = fixture.finish(identifier)  # No HTTP client is connected now.
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["result"]["search"]["budget"]["model_calls"], 0)
        fixture.reopen()
        status, history, _ = fixture.request("/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in history["items"]], [identifier])
        self.assertEqual(history["items"][0]["status"], "complete")
        self.assertEqual(history["items"][0]["quantity"], 9)
        status, restored, _ = fixture.request("/api/jobs/" + identifier)
        self.assertEqual(status, 200)
        self.assertEqual(restored["result"], completed["result"])
        self.assertEqual(fixture.db.quota_usage("search_runs"), 1)
        self.assertEqual(fixture.count("POST", "/api/search"), 1)

    def test_all_statuses_and_tied_date_pagination_exclude_snapshots_and_private_evidence(self):
        fixture = self.fixture
        statuses = ["running", "complete", "failed", "cancelled", "interrupted"]
        identifiers = [fixture.seed(i, statuses[(i - 1) % len(statuses)]) for i in range(1, 28)]
        fixture.seed(100, mode="snapshot")
        fixture.seed(101, mode="discovery")
        private = fixture.db.get_job(identifiers[1])
        private["result"]["html"] = "QA_PRIVATE_HTML"
        private["result"]["images"] = ["QA_PRIVATE_IMAGE"]
        private["result"]["evidence"] = {"raw": "QA_PRIVATE_EVIDENCE"}
        fixture.db.save_job(private)
        status, _, _ = fixture.request("/api/jobs?mode=snapshot")
        self.assertEqual(status, 400)  # History has no caller-selectable QA mode.
        status, default, _ = fixture.request("/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(default["total"], 27)
        self.assertEqual(len(default["items"]), 20)
        self.assertTrue(default["next_cursor"])
        status, page, _ = fixture.request("/api/jobs?limit=7")
        self.assertEqual(status, 200)
        found = list(page["items"])
        # Insertion ahead of an existing cursor must not shift subsequent pages.
        fixture.seed(200, created_at="2026-11-12T08:00:00Z")
        cursors = set()
        while page["next_cursor"]:
            cursor = page["next_cursor"]
            self.assertNotIn(cursor, cursors)
            cursors.add(cursor)
            status, page, _ = fixture.request("/api/jobs?limit=7&cursor=" + cursor)
            self.assertEqual(status, 200)
            self.assertEqual(page["total"], 28)
            found.extend(page["items"])
        self.assertEqual([item["id"] for item in found], list(reversed(identifiers)))
        self.assertEqual({item["status"] for item in found}, set(statuses))
        for item in found:
            self.assertTrue({"id", "status", "created_at", "updated_at", "revision", "request",
                             "description", "quantity", "offers_count", "calculation_status",
                             "total_price", "latest_event", "error"}.issubset(item))
            self.assertEqual(set(item["request"]), {"description", "quantity", "source_ids"})
            self.assertEqual(set(item["latest_event"]), {"time", "message", "kind"})
            self.assertNotIn("result", item)
            self.assertNotIn("offers", item)
        serialized = json.dumps(found)
        for forbidden in ["QA_PRIVATE_HTML", "QA_PRIVATE_IMAGE", "QA_PRIVATE_EVIDENCE", "base_result", "payload_json"]:
            self.assertNotIn(forbidden, serialized)

    def test_empty_history_and_bad_pagination_are_clear_json_responses(self):
        status, body, _ = self.fixture.request("/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"items": [], "next_cursor": None, "total": 0})
        for query in ["limit=0", "limit=-1", "limit=51", "limit=1.5", "limit=true",
                      "limit=", "cursor=not-a-valid-cursor", "cursor=W10", "cursor=e30"]:
            with self.subTest(query=query):
                status, body, headers = self.fixture.request("/api/jobs?" + query)
                self.assertEqual(status, 400)
                self.assertTrue(body.get("error"))
                self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(self.fixture.db.quota_usage("search_runs"), 0)

    def test_only_explicit_cancel_cancels_and_failure_remains_in_history(self):
        fixture = self.fixture
        cancelled = fixture.submit()
        fixture.request("/api/jobs")
        fixture.request("/api/jobs/" + cancelled["id"])
        self.assertFalse(fixture.controls[cancelled["id"]]["cancelled"].is_set())
        status, _, _ = fixture.request("/api/jobs/" + cancelled["id"] + "/cancel", {})
        self.assertEqual(status, 200)
        self.assertEqual(fixture.finish(cancelled["id"])["status"], "cancelled")
        failure_description = "QA: управляемый сбой исследования"
        fixture.fail_descriptions.add(failure_description)
        failed = fixture.submit(description=failure_description)
        self.assertEqual(fixture.finish(failed["id"])["status"], "failed")
        interrupted = fixture.seed(500, status="running")
        self.assertEqual(fixture.db.recover_interrupted(), 1)
        _, history, _ = fixture.request("/api/jobs")
        statuses = {item["id"]: item["status"] for item in history["items"]}
        self.assertEqual(statuses, {cancelled["id"]: "cancelled", failed["id"]: "failed", interrupted: "interrupted"})
        self.assertEqual(fixture.count("POST", "/api/jobs/" + cancelled["id"] + "/cancel"), 1)

    def test_two_background_workers_keep_server_concurrency_and_gets_do_not_reserve_quota(self):
        fixture = self.fixture
        first, second = fixture.submit(), fixture.submit(description=DESCRIPTION + " второй")
        status, body, _ = fixture.request("/api/search", {"description": DESCRIPTION, "source_ids": ["web"]})
        self.assertEqual(status, 429)
        self.assertTrue(body.get("error"))
        self.assertEqual(fixture.db.quota_usage("search_runs"), 2)
        for identifier in [first["id"], second["id"]]:
            self.assertEqual(fixture.request("/api/jobs/" + identifier)[0], 200)
            self.assertFalse(fixture.controls[identifier]["cancelled"].is_set())
        self.assertEqual(fixture.request("/api/jobs")[1]["total"], 2)
        self.assertEqual(fixture.db.quota_usage("search_runs"), 2)


if __name__ == "__main__":
    unittest.main()
