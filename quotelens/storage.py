"""Durable, thread-safe SQLite records for searches and their evidence.

JSON is the replay format; the related tables make decisions, evidence and
calculations inspectable without parsing a saved HTTP response.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import base64
import binascii
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import uuid
from urllib.parse import urlsplit


SCHEMA_VERSION = 3
_PRIVATE = {"cancel", "authorization", "api_key", "apikey", "access_token", "refresh_token",
            "secret", "password", "model_api_key", "search_api_key"}
_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_.~+/=-]+|\bsk-[A-Za-z0-9_-]{12,}")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe(value, depth=0):
    if depth > 30:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _TOKEN.sub("[redacted]", value)
    if isinstance(value, dict):
        return {str(k): _safe(v, depth + 1) for k, v in value.items()
                if str(k).casefold() not in _PRIVATE}
    if isinstance(value, (list, tuple)):
        return [_safe(v, depth + 1) for v in value]
    return None


def _dump(value):
    return json.dumps(_safe(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _load(value, default=None):
    try:
        return json.loads(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def _text(value):
    return value if isinstance(value, str) else None


def _request_summary(payload, result=None):
    """Only the original form fields belong in job metadata."""
    payload = payload if isinstance(payload, dict) else {}
    fallback = result.get("request", {}) if isinstance(result, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    description = payload.get("description")
    if not isinstance(description, str):
        description = fallback.get("description")
    quantity = payload.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity <= 0:
        quantity = fallback.get("quantity", 1)
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity <= 0:
        quantity = 1
    source_ids = payload.get("source_ids") if "source_ids" in payload else fallback.get("source_ids")
    if not isinstance(source_ids, list) or any(not isinstance(item, str) for item in source_ids):
        source_ids = None
    request = {"description": description if isinstance(description, str) else "",
               "quantity": quantity, "source_ids": source_ids}
    scope = payload.get("source_scope") if "source_scope" in payload else fallback.get("source_scope")
    if isinstance(scope, dict):
        from .source_policy import normalize_scope
        try:
            request["source_scope"] = normalize_scope(scope)
        except ValueError:
            pass
    for key in ("items", "search_policy"):
        if isinstance(payload.get(key), (dict, list)):
            request[key] = _safe(payload[key])
        elif key == "items" and isinstance(fallback.get("items"), list):
            request[key] = _safe(fallback[key])
    documents = payload.get("input_documents")
    if isinstance(documents, list):
        request["input_documents"] = [{key: _safe(doc.get(key)) for key in ("id", "name", "sha256")} for doc in documents if isinstance(doc, dict)]
    return request


def _history_cursor(created_at, identifier):
    value = json.dumps([created_at, identifier], ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(value.encode("utf-8")).rstrip(b"=").decode("ascii")


def _parse_history_cursor(cursor):
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
        raise ValueError("Некорректный указатель истории.")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
        if (not isinstance(value, list) or len(value) != 2
                or any(not isinstance(item, str) or not item for item in value)
                or _history_cursor(*value) != cursor):
            raise ValueError
        datetime.fromisoformat(value[0].replace("Z", "+00:00"))
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise ValueError("Некорректный указатель истории.") from None
    return value


class Store:
    def __init__(self, path):
        self.path = str(path)
        if self.path == ":memory:":
            # Connections are short lived, so a named memory DB needs an anchor.
            self.path = "file:quotelens-" + uuid.uuid4().hex + "?mode=memory&cache=shared"
            self._anchor = sqlite3.connect(self.path, uri=True, check_same_thread=False)
        elif not self.path.startswith("file:"):
            self.path = str(Path(self.path).expanduser().resolve())
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("Версия базы данных новее поддерживаемой.")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                    job_json TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', result_json TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_live_history ON jobs(created_at DESC,id DESC)
                    WHERE json_extract(payload_json,'$.mode')='live';
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL, event_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_job ON events(job_id,id);
                CREATE TABLE IF NOT EXISTS steps (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL, kind TEXT, tool TEXT, step_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS steps_job ON steps(job_id,created_at);
                CREATE TABLE IF NOT EXISTS documents (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, id TEXT NOT NULL,
                    url TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, html TEXT,
                    sha256 TEXT NOT NULL, retrieved_at TEXT NOT NULL, method TEXT,
                    document_json TEXT NOT NULL, PRIMARY KEY(job_id,id)
                );
                CREATE INDEX IF NOT EXISTS documents_hash ON documents(sha256);
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    job_id UNINDEXED, document_id UNINDEXED, title, text,
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TABLE IF NOT EXISTS offers (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, id TEXT NOT NULL,
                    title TEXT, supplier TEXT, url TEXT, document_id TEXT, source_hash TEXT,
                    price REAL, currency TEXT, unit TEXT, offer_json TEXT NOT NULL,
                    PRIMARY KEY(job_id,id)
                );
                CREATE INDEX IF NOT EXISTS offers_supplier ON offers(supplier);
                CREATE TABLE IF NOT EXISTS decisions (
                    job_id TEXT NOT NULL, offer_id TEXT NOT NULL, status TEXT NOT NULL,
                    normalized_price REAL, duplicate_of TEXT, manual_excluded INTEGER NOT NULL,
                    reasons_json TEXT NOT NULL, PRIMARY KEY(job_id,offer_id),
                    FOREIGN KEY(job_id,offer_id) REFERENCES offers(job_id,id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    job_id TEXT NOT NULL, offer_id TEXT NOT NULL, field TEXT NOT NULL,
                    quote TEXT NOT NULL, url TEXT, document_id TEXT, source_hash TEXT,
                    evidence_json TEXT NOT NULL, PRIMARY KEY(job_id,offer_id,field),
                    FOREIGN KEY(job_id,offer_id) REFERENCES offers(job_id,id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS evidence_document ON evidence(job_id,document_id);
                CREATE TABLE IF NOT EXISTS groups (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL, id TEXT NOT NULL, label TEXT, group_json TEXT NOT NULL,
                    PRIMARY KEY(job_id,kind,id)
                );
                CREATE TABLE IF NOT EXISTS calculations (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    status TEXT, n INTEGER, independent_suppliers INTEGER, mean REAL,
                    unit_price REAL, total REAL, calculation_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL, offer_id TEXT NOT NULL, action TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(job_id,revision)
                );
                CREATE TABLE IF NOT EXISTS review_overrides (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    offer_id TEXT NOT NULL, confirmations_json TEXT NOT NULL,
                    PRIMARY KEY(job_id,offer_id)
                );
                CREATE TABLE IF NOT EXISTS review_acceptances (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    offer_id TEXT NOT NULL, acceptances_json TEXT NOT NULL,
                    PRIMARY KEY(job_id,offer_id)
                );
                CREATE TABLE IF NOT EXISTS shared_results (
                    token TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    revision INTEGER NOT NULL, created_at TEXT NOT NULL,
                    result_json TEXT NOT NULL, documents_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quotas (
                    kind TEXT NOT NULL, period TEXT NOT NULL, period_key TEXT NOT NULL,
                    used INTEGER NOT NULL CHECK(used>=0), PRIMARY KEY(kind,period,period_key)
                );
                CREATE TABLE IF NOT EXISTS quarantine (
                    id INTEGER PRIMARY KEY, job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE,
                    entity TEXT NOT NULL, reason TEXT NOT NULL, value_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            if "fields_json" not in {row[1] for row in db.execute("PRAGMA table_info(reviews)")}:
                db.execute("ALTER TABLE reviews ADD COLUMN fields_json TEXT")
            db.execute("PRAGMA user_version=3")

    @contextmanager
    def _connection(self, immediate=False):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=20, uri=self.path.startswith("file:"))
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=20000")
            try:
                if immediate:
                    db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def _job_id(job):
        if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not job["id"]:
            raise ValueError("У анализа должен быть непустой идентификатор.")
        return job["id"]

    def create_job(self, job, payload):
        identifier = self._job_id(job)
        clean = _safe(job)
        if payload.get("mode") == "live":
            clean["pass_context"] = {"number": 1, "started_at": clean.get("created_at") or _now(), "previous": []}
        with self._connection(immediate=True) as db:
            db.execute("INSERT INTO jobs(id,status,created_at,updated_at,job_json,payload_json) VALUES(?,?,?,?,?,?)",
                       (identifier, str(clean.get("status", "running")), clean.get("created_at") or _now(),
                        _now(), _dump(clean), _dump(payload)))
            self._replace_events(db, identifier, clean.get("events", []))
            if isinstance(clean.get("result"), dict):
                self._save_result(db, identifier, job["result"])

    @staticmethod
    def _replace_events(db, job_id, events):
        if not isinstance(events, list):
            return
        db.execute("DELETE FROM events WHERE job_id=?", (job_id,))
        for event in events:
            if isinstance(event, dict):
                db.execute("INSERT INTO events(job_id,created_at,event_json) VALUES(?,?,?)",
                           (job_id, event.get("time") or _now(), _dump(event)))

    def save_job(self, job):
        identifier = self._job_id(job)
        clean = _safe(job)
        with self._connection(immediate=True) as db:
            row = db.execute("SELECT id,job_json FROM jobs WHERE id=?", (identifier,)).fetchone()
            if row and "pass_context" not in clean:
                previous = _load(row["job_json"], {})
                if previous.get("pass_context"):
                    clean["pass_context"] = previous["pass_context"]
            if row and "search_progress" not in clean:
                previous = _load(row["job_json"], {})
                if isinstance(previous.get("search_progress"), dict):
                    clean["search_progress"] = previous["search_progress"]
            if row is None:
                db.execute("INSERT INTO jobs(id,status,created_at,updated_at,job_json) VALUES(?,?,?,?,?)",
                           (identifier, str(clean.get("status", "running")), clean.get("created_at") or _now(), _now(), _dump(clean)))
            else:
                db.execute("UPDATE jobs SET status=?,updated_at=?,job_json=? WHERE id=?",
                           (str(clean.get("status", "running")), _now(), _dump(clean), identifier))
            if "events" in clean:
                self._replace_events(db, identifier, clean["events"])
            if isinstance(clean.get("result"), dict):
                self._save_result(db, identifier, job["result"])

    def get_job(self, job_id):
        with self._connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            job = _load(row["job_json"], {})
            job.update(id=row["id"], status=row["status"], revision=row["revision"])
            job["events"] = [_load(item[0], {}) for item in db.execute("SELECT event_json FROM events WHERE job_id=? ORDER BY id", (job_id,))]
            if row["result_json"] is not None:
                job["result"] = _load(row["result_json"], {})
            job["request"] = _request_summary(_load(row["payload_json"], {}), job.get("result"))
            reviews = [dict(item) for item in db.execute(
                "SELECT revision,offer_id,action,reason,created_at,fields_json FROM reviews WHERE job_id=? ORDER BY revision", (job_id,))]
            excluded = set((job.get("result") or {}).get("review", {}).get("excluded_ids", []))
            for item in reviews:
                if item["action"] == "exclude":
                    excluded.add(item["offer_id"])
                elif item["action"] == "reset":
                    excluded.discard(item["offer_id"])
                fields = item.pop("fields_json", None)
                if fields:
                    item["fields"] = _load(fields, {})
            job["reviews"] = reviews
            job["excluded_ids"] = sorted(excluded)
            job["manual_overrides"] = {item["offer_id"]: _load(item["confirmations_json"], {}) for item in db.execute(
                "SELECT offer_id,confirmations_json FROM review_overrides WHERE job_id=?", (job_id,))}
            job["manual_acceptances"] = {item["offer_id"]: _load(item["acceptances_json"], {}) for item in db.execute(
                "SELECT offer_id,acceptances_json FROM review_acceptances WHERE job_id=?", (job_id,))}
            return job

    def list_jobs(self, limit=20):
        limit = max(1, min(100, int(limit)))
        with self._connection() as db:
            identifiers = [row[0] for row in db.execute("SELECT id FROM jobs ORDER BY created_at DESC,id DESC LIMIT ?", (limit,))]
        return [self.get_job(identifier) for identifier in identifiers]

    def discovery_urls(self, request, limit=9):
        """Prior verified live sources are discovery hints, never cached offers."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 12:
            raise ValueError("Недопустимый размер списка источников.")

        def signature(value):
            category = value.get("category")
            requirements = [{key: item.get(key) for key in ("key", "op", "value", "unit")}
                            for item in value.get("requirements", []) if isinstance(item, dict)]
            requirements.sort(key=lambda item: str(item.get("key")))
            description = re.sub(r"\s+", " ", value.get("description", "").casefold()).strip() if category in {None, "other"} else ""
            return _dump([category, requirements, description])

        wanted = signature(request)
        grouped, seen = {}, set()
        with self._connection() as db:
            rows = db.execute("""SELECT id,result_json FROM jobs WHERE status='complete'
                AND result_json IS NOT NULL AND json_extract(payload_json,'$.mode')='live'
                ORDER BY created_at DESC,id DESC LIMIT 40""").fetchall()
            for row in rows:
                result = _load(row["result_json"], {})
                roots = [(result, None)] + [(item, item.get("id")) for item in result.get("items", []) if isinstance(item, dict)]
                root_excluded = set(result.get("review", {}).get("excluded_ids", []))
                for line, item_id in roots:
                    if signature(line.get("request", {})) != wanted:
                        continue
                    excluded = root_excluded | set(line.get("review", {}).get("excluded_ids", []))
                    for offer in line.get("offers", []):
                        url = offer.get("url", "")
                        parsed = urlsplit(url)
                        domain = (parsed.hostname or "").removeprefix("www.").lower()
                        identifier = str(offer.get("id") or "")
                        prefixed = str(item_id) + ":" + identifier if item_id else identifier
                        price, proof = offer.get("price"), offer.get("evidence", {}).get("price", {})
                        if (url in seen or parsed.scheme != "https" or not domain or parsed.username or parsed.password
                                or identifier in excluded or prefixed in excluded or offer.get("status") == "excluded"
                                or offer.get("provenance") != "live" or isinstance(price, bool)
                                or not isinstance(price, (float, int)) or not math.isfinite(price) or price <= 0
                                or not isinstance(proof, dict) or not proof.get("quote")
                                or proof.get("url") != url or not proof.get("document_id")):
                            continue
                        doc = db.execute("SELECT url,sha256 FROM documents WHERE job_id=? AND id=?",
                                         (row["id"], proof["document_id"])).fetchone()
                        if not doc or doc["url"] != url or doc["sha256"] != proof.get("source_hash"):
                            continue
                        seen.add(url)
                        queue = grouped.setdefault(domain, [])
                        if len(queue) < 3:
                            queue.append({"url": url, "title": str(offer.get("title") or "")[:300], "source_job_id": row["id"]})
        hints = []
        while grouped and len(hints) < limit:
            for domain in list(grouped):
                hints.append(grouped[domain].pop(0))
                if not grouped[domain]:
                    del grouped[domain]
                if len(hints) >= limit:
                    break
        return hints

    def job_history(self, limit=20, cursor=None):
        """Read a bounded live-search summary page without loading job results."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("Размер страницы истории должен быть от 1 до 50.")
        position = _parse_history_cursor(cursor) if cursor is not None else None
        where = "json_extract(j.payload_json,'$.mode')='live'"
        parameters = []
        if position is not None:
            where += " AND (j.created_at,j.id)<(?,?)"
            parameters.extend(position)
        parameters.append(limit + 1)
        with self._connection() as db:
            # Count and page see the same SQLite snapshot, including during writes
            # by another Store instance or a worker in another process.
            db.execute("BEGIN")
            total = db.execute("SELECT count(*) FROM jobs WHERE json_extract(payload_json,'$.mode')='live'").fetchone()[0]
            rows = db.execute("""SELECT j.id,j.status,j.created_at,j.updated_at,j.revision,
                COALESCE(CASE WHEN json_type(j.payload_json,'$.description')='text'
                              THEN json_extract(j.payload_json,'$.description') END,
                         CASE WHEN json_type(j.result_json,'$.request.description')='text'
                              THEN json_extract(j.result_json,'$.request.description') END) AS description,
                COALESCE(CASE WHEN json_type(j.payload_json,'$.quantity') IN ('integer','real')
                              THEN json_extract(j.payload_json,'$.quantity') END,
                         CASE WHEN json_type(j.result_json,'$.request.quantity') IN ('integer','real')
                              THEN json_extract(j.result_json,'$.request.quantity') END,1) AS quantity,
                CASE WHEN json_type(j.payload_json,'$.source_ids') IS NOT NULL
                     THEN CASE WHEN json_type(j.payload_json,'$.source_ids')='array'
                               THEN json_extract(j.payload_json,'$.source_ids') END
                     ELSE CASE WHEN json_type(j.result_json,'$.request.source_ids')='array'
                               THEN json_extract(j.result_json,'$.request.source_ids') END END AS source_ids,
                CASE WHEN json_type(j.payload_json,'$.source_scope')='object'
                     THEN json_extract(j.payload_json,'$.source_scope') END AS source_scope,
                (SELECT count(*) FROM offers WHERE job_id=j.id) AS offers_count,
                c.status AS calculation_status,c.total AS total_price,
                CASE WHEN json_type(j.job_json,'$.error')='text'
                     THEN json_extract(j.job_json,'$.error') END AS error,
                e.created_at AS event_time,
                CASE WHEN json_type(e.event_json,'$.message')='text'
                     THEN json_extract(e.event_json,'$.message') END AS event_message,
                CASE WHEN json_type(e.event_json,'$.kind')='text'
                     THEN json_extract(e.event_json,'$.kind') END AS event_kind
                FROM jobs j LEFT JOIN calculations c ON c.job_id=j.id
                LEFT JOIN events e ON e.id=(SELECT id FROM events WHERE job_id=j.id ORDER BY id DESC LIMIT 1)
                WHERE """ + where + " ORDER BY j.created_at DESC,j.id DESC LIMIT ?", parameters).fetchall()
        items = []
        for row in rows[:limit]:
            item = {key: row[key] for key in ("id", "status", "created_at", "updated_at", "revision",
                    "offers_count", "calculation_status", "total_price", "error")}
            original = {"description": row["description"], "quantity": row["quantity"],
                        "source_ids": _load(row["source_ids"])}
            if row["source_scope"] is not None:
                original["source_scope"] = _load(row["source_scope"])
            request = _request_summary(original)
            item.update(request=request, description=request["description"], quantity=request["quantity"])
            item["latest_event"] = ({"time": row["event_time"], "message": row["event_message"], "kind": row["event_kind"]}
                                    if row["event_time"] is not None else None)
            items.append(item)
        next_cursor = _history_cursor(rows[limit - 1]["created_at"], rows[limit - 1]["id"]) if len(rows) > limit else None
        return {"items": items, "next_cursor": next_cursor, "total": total}

    def recover_interrupted(self):
        with self._connection(immediate=True) as db:
            rows = db.execute("SELECT id,job_json FROM jobs WHERE status IN ('running','queued')").fetchall()
            for row in rows:
                job = _load(row["job_json"], {})
                reason = "Поиск прерван перезапуском сервера; сохранённые данные доступны, автоматический повтор не выполнялся."
                job.update(status="interrupted", error=reason)
                db.execute("UPDATE jobs SET status='interrupted',updated_at=?,job_json=? WHERE id=?", (_now(), _dump(job), row["id"]))
                db.execute("INSERT INTO events(job_id,created_at,event_json) VALUES(?,?,?)",
                           (row["id"], _now(), _dump({"time": _now(), "kind": "warning", "message": reason})))
            return len(rows)

    def add_event(self, job_id, event):
        if not isinstance(event, dict):
            raise ValueError("Событие должно быть объектом.")
        with self._connection() as db:
            db.execute("INSERT INTO events(job_id,created_at,event_json) VALUES(?,?,?)", (job_id, event.get("time") or _now(), _dump(event)))

    def set_search_progress(self, job_id, progress):
        """Persist bounded live traversal without copying raw pages to clients."""
        if not isinstance(progress, dict):
            raise ValueError("Некорректный ход исследования.")
        safe = _safe(progress)
        safe["sources"] = safe.get("sources", [])[:80] if isinstance(safe.get("sources"), list) else []
        with self._connection(immediate=True) as db:
            row = db.execute("SELECT job_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("Исследование не найдено.")
            current = _load(row["job_json"], {})
            current["search_progress"] = safe
            db.execute("UPDATE jobs SET job_json=?,updated_at=? WHERE id=?", (_dump(current), _now(), job_id))

    def extraction_document_ids(self, job_id, item_id):
        """Documents actually processed for this line, from durable tool audit."""
        with self._connection() as db:
            rows = db.execute("SELECT step_json FROM steps WHERE job_id=? ORDER BY rowid", (job_id,)).fetchall()
        found = set()
        for row in rows:
            step = _load(row[0], {})
            if step.get("item_id") != item_id:
                continue
            if step.get("name") == "extract_product_offer" and isinstance(step.get("document_id"), str):
                found.add(step["document_id"])
            if step.get("name") == "open_page" and isinstance(step.get("output"), dict):
                value = step["output"].get("document_id")
                if isinstance(value, str):
                    found.add(value)
        return found

    def add_step(self, job_id, step):
        if not isinstance(step, dict):
            raise ValueError("Шаг должен быть объектом.")
        identifier = uuid.uuid4().hex
        with self._connection() as db:
            db.execute("INSERT INTO steps(id,job_id,created_at,kind,tool,step_json) VALUES(?,?,?,?,?,?)",
                       (identifier, job_id, _now(), _text(step.get("kind")), _text(step.get("tool") or step.get("name")), _dump(step)))
        return identifier

    def add_document(self, job_id, document):
        if not isinstance(document, dict) or not isinstance(document.get("text"), str) or not isinstance(document.get("url"), str) or not document["url"]:
            raise ValueError("Документ должен содержать адрес и сохранённый текст.")
        document = _safe(document)
        raw = document.get("html") if isinstance(document.get("html"), str) else document["text"]
        digest = document.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            digest = hashlib.sha256(raw.encode()).hexdigest()
        identifier = hashlib.sha256((document["url"] + "\n" + digest).encode()).hexdigest()[:24]
        document.update(id=identifier, document_id=identifier, sha256=digest, title=str(document.get("title") or ""),
                        retrieved_at=document.get("retrieved_at") or _now())
        with self._connection(immediate=True) as db:
            db.execute("""INSERT INTO documents(job_id,id,url,title,text,html,sha256,retrieved_at,method,document_json)
                          VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id,id) DO UPDATE SET document_json=excluded.document_json""",
                       (job_id, identifier, document["url"], document["title"], document["text"], document.get("html"),
                        digest, document["retrieved_at"], document.get("method"), _dump(document)))
            db.execute("DELETE FROM documents_fts WHERE job_id=? AND document_id=?", (job_id, identifier))
            db.execute("INSERT INTO documents_fts(job_id,document_id,title,text) VALUES(?,?,?,?)",
                       (job_id, identifier, document["title"], document["text"]))
        return identifier

    def get_document(self, job_id, docid):
        with self._connection() as db:
            row = db.execute("SELECT document_json FROM documents WHERE job_id=? AND id=?", (job_id, docid)).fetchone()
            return _load(row[0]) if row else None

    def list_documents(self, job_id):
        with self._connection() as db:
            return [dict(row) for row in db.execute("""SELECT id,id AS document_id,url,title,sha256,retrieved_at,method,
                length(text) AS text_length,length(html) AS html_length FROM documents WHERE job_id=? ORDER BY rowid""", (job_id,))]

    def search_documents(self, job_id, query, limit=6):
        # Quote tokens ourselves: incoming model text cannot become FTS syntax.
        tokens = re.findall(r"[^\W_]+", str(query or ""), re.UNICODE)[:16]
        if not tokens:
            return []
        match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        with self._connection() as db:
            rows = db.execute("""SELECT document_id AS id,document_id,title,
                snippet(documents_fts,3,'','', ' … ',40) AS snippet,bm25(documents_fts) AS score
                FROM documents_fts WHERE documents_fts MATCH ? AND job_id=? ORDER BY score LIMIT ?""",
                              (match, job_id, max(1, min(20, int(limit))))).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                meta = db.execute("SELECT url,sha256,retrieved_at FROM documents WHERE job_id=? AND id=?", (job_id, item["id"])).fetchone()
                if meta:
                    item.update(dict(meta))
                results.append(item)
            return results

    @staticmethod
    def _quarantine(db, job_id, entity, reason, value):
        db.execute("INSERT INTO quarantine(job_id,entity,reason,value_json,created_at) VALUES(?,?,?,?,?)",
                   (job_id, entity, reason, _dump(value), _now()))

    def _save_result(self, db, job_id, result):
        row = db.execute("SELECT job_json,status,revision FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row:
            result = self._pass_result(result, _load(row["job_json"], {}), row["status"])
            overrides = {r[0]: _load(r[1], {}) for r in db.execute("SELECT offer_id,confirmations_json FROM review_overrides WHERE job_id=?", (job_id,))}
            accepted = {r[0]: _load(r[1], {}) for r in db.execute("SELECT offer_id,acceptances_json FROM review_acceptances WHERE job_id=?", (job_id,))}
            excluded = set(result.get("review", {}).get("excluded_ids", []))
            for decision in db.execute("SELECT offer_id,action FROM reviews WHERE job_id=? ORDER BY revision", (job_id,)):
                if decision["action"] == "exclude":
                    excluded.add(decision["offer_id"])
                elif decision["action"] == "reset":
                    excluded.discard(decision["offer_id"])
            if (overrides or accepted or excluded) and row["status"] == "running":
                from .review import rebuild_result
                result = rebuild_result(result, overrides, excluded, row["revision"], accepted)
        raw_result = result
        result = _safe(result)
        db.execute("UPDATE jobs SET result_json=?,updated_at=? WHERE id=?", (_dump(result), _now(), job_id))
        for table in ("offers", "groups", "calculations"):
            db.execute("DELETE FROM " + table + " WHERE job_id=?", (job_id,))
        offers = result.get("offers", [])
        if not isinstance(offers, list):
            self._quarantine(db, job_id, "offers", "Ожидался список предложений.", offers)
            offers = []
        seen = set()
        for index, offer in enumerate(offers):
            if not isinstance(offer, dict) or not isinstance(offer.get("id"), str) or not offer["id"] or offer["id"] in seen:
                self._quarantine(db, job_id, "offer:" + str(index), "Некорректное или повторное предложение.", offer)
                continue
            seen.add(offer["id"])
            price = offer.get("price")
            raw_offers = raw_result.get("offers", [])
            raw_offer = raw_offers[index] if isinstance(raw_offers, list) and index < len(raw_offers) else None
            if isinstance(raw_offer, dict) and isinstance(raw_offer.get("price"), float) and not math.isfinite(raw_offer["price"]):
                self._quarantine(db, job_id, "offer:" + offer["id"] + ":price", "Числовая цена не является конечной.", None)
            if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0):
                self._quarantine(db, job_id, "offer:" + offer["id"] + ":price", "Некорректная числовая цена.", price)
                price = None
            for field in ("title", "supplier", "url", "document_id", "source_hash", "currency", "unit"):
                if offer.get(field) is not None and not isinstance(offer[field], str):
                    self._quarantine(db, job_id, "offer:" + offer["id"] + ":" + field, "Ожидалось текстовое поле.", offer[field])
            db.execute("""INSERT INTO offers(job_id,id,title,supplier,url,document_id,source_hash,price,currency,unit,offer_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (job_id, offer["id"], _text(offer.get("title")), _text(offer.get("supplier")), _text(offer.get("url")),
                _text(offer.get("document_id")), _text(offer.get("source_hash")), price, _text(offer.get("currency")), _text(offer.get("unit")), _dump(offer)))
            status = offer.get("status", "uncertain")
            if not isinstance(status, str) or status not in {"included", "excluded", "uncertain"}:
                self._quarantine(db, job_id, "decision:" + offer["id"], "Неизвестный статус решения.", status)
                status = "uncertain"
            normalized = offer.get("normalized_price")
            normalized = normalized if isinstance(normalized, (int, float)) and not isinstance(normalized, bool) else None
            db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?)", (job_id, offer["id"], status, normalized,
                       _text(offer.get("duplicate_of")), int(bool(offer.get("manual_excluded"))), _dump(offer.get("reasons", []))))
            evidence = offer.get("evidence", {})
            if not isinstance(evidence, dict):
                self._quarantine(db, job_id, "evidence:" + offer["id"], "Ожидался объект доказательств.", evidence)
                continue
            for field, item in evidence.items():
                dom_image = (field == "image_url" and isinstance(item, dict) and item.get("basis") == "primary_product_dom"
                             and all(isinstance(item.get(key), str) and item[key].strip() for key in ("locator", "attribute", "raw_value"))
                             and bool(item.get("document_id") or offer.get("document_id"))
                             and bool(item.get("source_hash") or offer.get("source_hash")))
                if not isinstance(item, dict) or not dom_image and (not isinstance(item.get("quote"), str) or not item["quote"].strip()):
                    self._quarantine(db, job_id, "evidence:" + offer["id"] + ":" + field, "Нет текстовой цитаты.", item)
                    continue
                db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?)", (job_id, offer["id"], field, item.get("quote") or "", _text(item.get("url")),
                           _text(item.get("document_id") or offer.get("document_id")), _text(item.get("source_hash") or offer.get("source_hash")), _dump(item)))
        groups = result.get("groups", {})
        if isinstance(groups, list):
            groups = {"group": groups}
        if isinstance(groups, dict):
            for kind, items in groups.items():
                if not isinstance(items, list):
                    continue
                for index, group in enumerate(items):
                    if isinstance(group, dict):
                        db.execute("INSERT OR REPLACE INTO groups VALUES(?,?,?,?,?)", (job_id, kind,
                                   str(group.get("id") or index), _text(group.get("label") or group.get("name")), _dump(group)))
        calculation = result.get("calculation")
        if isinstance(calculation, dict):
            numeric = lambda key: calculation.get(key) if isinstance(calculation.get(key), (int, float)) and not isinstance(calculation.get(key), bool) else None
            db.execute("INSERT INTO calculations VALUES(?,?,?,?,?,?,?,?)", (job_id, _text(calculation.get("status")), numeric("n"),
                       numeric("independent_suppliers"), numeric("mean"), numeric("unit_price"), numeric("total"), _dump(calculation)))

    def save_result(self, job_id, result):
        if not isinstance(result, dict):
            raise ValueError("Результат должен быть объектом.")
        with self._connection(immediate=True) as db:
            if db.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone() is None:
                raise ValueError("Анализ не найден.")
            self._save_result(db, job_id, result)

    @staticmethod
    def _pass_result(result, job, status):
        """A pass budget is incremental; the visible budget includes all passes."""
        context = job.get("pass_context")
        if not context or not isinstance(result.get("search"), dict):
            return result
        result = _safe(result)
        search = result["search"]
        current = search.get("current_pass_budget") or dict(search.get("budget", {}))
        previous = context.get("previous", [])
        record = {"number": context["number"], "started_at": context["started_at"],
                  "finished_at": search.get("finished_at") if status != "running" else None,
                  "stop": search.get("stop") if status != "running" else None, "budget": current}
        search["current_pass_budget"] = current
        search["passes"] = [*previous, record]
        cumulative = dict(current)
        for key in ("pages_used", "queries_used", "model_calls", "elapsed_seconds", "reserved_tokens", "reserved_micro_usd", "settled_upper_micro_usd"):
            if isinstance(current.get(key), (int, float)):
                cumulative[key] = current[key] + sum(p.get("budget", {}).get(key, 0) or 0 for p in previous)
        usages = [p.get("budget", {}) for p in [*previous, record]]
        cumulative["actual_cost_known"] = all(b.get("actual_cost_known") is True or b.get("model_calls", 0) == 0 for b in usages)
        cumulative["actual_usage"] = {key: sum(b.get("actual_usage", {}).get(key, 0) or 0 for b in usages)
                                      for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost")}
        search["budget"] = cumulative
        return result

    def begin_continuation(self, job_id, max_seconds, expected_revision):
        from .review import RevisionConflict
        if isinstance(max_seconds, bool) or not isinstance(max_seconds, int) or not 1 <= max_seconds <= 3600:
            raise ValueError("Время продолжения должно быть от 1 до 3600 секунд.")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("Для продолжения нужна текущая ревизия.")
        with self._connection(immediate=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("Исследование не найдено.")
            if row["revision"] != expected_revision:
                raise RevisionConflict(row["revision"])
            if row["status"] == "running" or not row["result_json"]:
                raise ValueError("Дождитесь завершения текущего поиска.")
            payload, result = _load(row["payload_json"], {}), _load(row["result_json"], {})
            if payload.get("mode") != "live" or not result.get("items"):
                raise ValueError("Продолжение доступно для исследований позиций закупки.")
            # Repaired legacy jobs may have structured source-backed results
            # but only the original free-text form in payload_json. The saved
            # result manifest, not another parse of truncated text, is resumed.
            from .spec_ingest import normalize_items
            current_items = result.get("request", {}).get("items")
            if not current_items:
                current_items = []
                for line in result["items"]:
                    req = (line.get("base_result") or {}).get("request") or line.get("request") or {}
                    current_items.append({**req, "id": line.get("id"), "title": line.get("title") or req.get("product"),
                                          "quantity": line.get("quantity", req.get("quantity")), "unit": line.get("unit", req.get("unit"))})
            requests = {line.get("id"): (line.get("base_result") or {}).get("request") or line.get("request") or {} for line in result["items"]}
            current_items = _safe(current_items)
            for item in current_items:
                req = requests.get(item.get("id"), {})
                if item.get("requirements") is None:
                    if isinstance(req.get("requirements"), list):
                        item["requirements"] = _safe(req["requirements"])
                    else:
                        item.pop("requirements", None)
                if item.get("conditions") is None:
                    item["conditions"] = _safe(req.get("conditions") or [])
                if item.get("warnings") is None:
                    item["warnings"] = []
                if item.get("source") is None:
                    item.pop("source", None)
            resumed_items = normalize_items(current_items)
            if not resumed_items or {i["id"] for i in resumed_items} != {i.get("id") for i in result["items"]}:
                raise ValueError("Старый результат не содержит полного списка позиций; подготовьте закупку заново.")
            original_payload = _safe(payload)
            payload["items"] = resumed_items
            from .agent import LIMITS
            payload["limits"] = {**LIMITS, **payload.get("limits", {})}
            previous = result.get("search", {}).get("passes") or [{"number": 1,
                "started_at": result.get("search", {}).get("started_at", row["created_at"]),
                "finished_at": result.get("search", {}).get("finished_at"),
                "stop": result.get("search", {}).get("stop"), "budget": result.get("search", {}).get("budget", {})}]
            revision, at = row["revision"] + 1, _now()
            job = _load(row["job_json"], {})
            job.pop("result", None)
            job.pop("error", None)
            job.pop("search_progress", None)
            job.update(status="running", revision=revision, pass_context={"number": len(previous) + 1, "started_at": at, "previous": previous})
            db.execute("UPDATE jobs SET status='running',revision=?,updated_at=?,job_json=? WHERE id=?", (revision, at, _dump(job), job_id))
            db.execute("INSERT INTO steps(id,job_id,created_at,kind,tool,step_json) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, job_id, at, "continuation", "begin", _dump({"before_revision": expected_revision, "max_seconds": max_seconds, "before_result": result, "before_payload": original_payload})))
            from .procurement import normalize_search_policy
            payload["search_policy"] = normalize_search_policy({"mode": "target", "max_seconds": max_seconds})
            payload["_resume_result"] = result
            # Old runs predate continuation_state. Their immutable tool steps
            # and captures still provide exact query, visited and link state.
            document_items, queries, seen_queries = {}, [], set()
            for item in result["items"]:
                for offer in (item.get("base_result") or item).get("offers", []):
                    if offer.get("document_id"):
                        document_items.setdefault(offer["document_id"], set()).add(item["id"])
            for step in db.execute("SELECT tool,json_extract(step_json,'$.item_id') AS item_id,json_extract(step_json,'$.arguments.query') AS query,coalesce(json_extract(step_json,'$.document_id'),json_extract(step_json,'$.output.document_id')) AS document_id FROM steps WHERE job_id=? ORDER BY created_at,id", (job_id,)):
                if step["tool"] == "search_web" and isinstance(step["query"], str):
                    key = (step["item_id"], step["query"].casefold())
                    if key not in seen_queries:
                        queries.append({"item_id": step["item_id"], "query": step["query"]})
                        seen_queries.add(key)
                if step["document_id"] and step["item_id"]:
                    document_items.setdefault(step["document_id"], set()).add(step["item_id"])
            payload["_resume_queries"] = queries
            documents = []
            for record in db.execute("SELECT id AS document_id,url,sha256,retrieved_at,method,json_extract(document_json,'$.page_kind') AS page_kind,json_extract(document_json,'$.product_links') AS product_links_json,json_extract(document_json,'$.links') AS links_json FROM documents WHERE job_id=?", (job_id,)):
                doc = dict(record)
                doc["product_links"] = _load(doc.pop("product_links_json"), [])
                doc["links"] = _load(doc.pop("links_json"), [])
                doc["item_ids"] = sorted(document_items.get(doc["document_id"], set()))
                documents.append(doc)
            payload["_resume_documents"] = documents
        return payload

    def cost_report(self):
        """Count provider receipts, including discovery calls, without raw prompts."""
        with self._connection() as db:
            jobs = [dict(r) for r in db.execute("SELECT id,status,created_at,payload_json,json_extract(result_json,'$.search.budget.model_calls') AS expected_calls,json_extract(result_json,'$.request.product') AS product,json_extract(result_json,'$.search.passes') AS passes_json FROM jobs ORDER BY created_at DESC,id DESC")]
            receipts = {}
            for row in db.execute("SELECT job_id,kind,coalesce(json_extract(step_json,'$.usage'),json_extract(step_json,'$.output.usage')) AS usage FROM steps WHERE kind IN ('model','discovery')"):
                receipts.setdefault(row["job_id"], []).append(_load(row["usage"], {}))
        output = []
        for job in jobs:
            payload = _load(job.pop("payload_json"), {})
            uses = receipts.get(job["id"], [])
            expected = job.pop("expected_calls")
            calls = max(expected if isinstance(expected, int) else 0, len(uses))
            costs, valid_tokens = [], True
            sums = {key: 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
            for usage in uses:
                prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
                valid = all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (prompt, completion))
                if valid:
                    valid = usage.get("total_tokens", prompt + completion) == prompt + completion
                valid_tokens &= valid
                if valid:
                    sums["prompt_tokens"] += prompt
                    sums["completion_tokens"] += completion
                    sums["total_tokens"] += prompt + completion
                cost = usage.get("cost")
                if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
                    costs.append(Decimal(str(cost)))
            known = len(costs) == calls and valid_tokens and job["status"] != "running"
            product = job.pop("product")
            passes = _load(job.pop("passes_json"), [])
            if not passes:
                passes = [{"number": 1, "started_at": job["created_at"], "legacy": True,
                           "budget": {"model_calls": calls, "actual_cost_known": known,
                                      "actual_usage": {**sums, "cost": float(sum(costs, Decimal(0))) if known else None}}}]
            item_title = " · ".join(str(i.get("title") or "") for i in payload.get("items", [])[:3])
            title = (product if product and product.casefold() != "закупка" else None) or item_title or str(payload.get("description") or "Исследование").splitlines()[0][:120]
            output.append({**job, "title": title[:180], "mode": payload.get("mode"), "model_calls": calls, "passes": passes,
                "metered_calls": len(uses), **sums, "cost_usd": float(sum(costs, Decimal(0))) if known else None,
                "known_cost_usd": float(sum(costs, Decimal(0))), "cost_known": known, "tokens_known": valid_tokens and len(uses) == calls})
        totals = {key: sum(j[key] for j in output) for key in ("model_calls", "metered_calls", "prompt_tokens", "completion_tokens", "total_tokens")}
        totals.update(cost_usd=float(sum((Decimal(str(j["known_cost_usd"])) for j in output), Decimal(0))), cost_known=all(j["cost_known"] for j in output), jobs=len(output))
        return {"jobs": output, "totals": totals, "source": "Сохранённые ответы провайдера: usage.cost, включая поисковые запросы; это не счёт за инфраструктуру.", "at": _now()}

    def search_audit(self, job_id):
        with self._connection() as db:
            queries = []
            for row in db.execute("SELECT json_extract(step_json,'$.arguments.query') AS query FROM steps WHERE job_id=? AND tool='search_web' ORDER BY created_at,id", (job_id,)):
                if isinstance(row[0], str) and row[0] not in queries:
                    queries.append(row[0])
            pages = [dict(row) for row in db.execute("SELECT id AS document_id,url,title,sha256,retrieved_at,method,json_extract(document_json,'$.page_kind') AS page_kind FROM documents WHERE job_id=? AND coalesce(method,'')!='search_api' ORDER BY retrieved_at,id", (job_id,))]
        return {"queries": queries, "checked_pages": pages,
                "conclusion": "Вывод относится к проверенным источникам. Отсутствие предложений на всём рынке не установлено."}

    def create_share(self, job_id, expected_revision, result=None):
        from .review import RevisionConflict
        from .sharing import sanitize_snapshot
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("Для ссылки нужна текущая ревизия.")
        with self._connection(immediate=True) as db:
            row = db.execute("SELECT revision,status,result_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or not row["result_json"]:
                raise ValueError("Результат не найден.")
            if row["revision"] != expected_revision:
                raise RevisionConflict(row["revision"])
            if row["status"] == "running":
                raise ValueError("Дождитесь завершения поиска.")
            # The caller may supply presentation annotations only while holding
            # the app lock; the authoritative revision is checked here.
            result = result or _load(row["result_json"], {})
            documents = [_load(r[0], {}) for r in db.execute("SELECT document_json FROM documents WHERE job_id=? ORDER BY retrieved_at,id", (job_id,))]
            audit = self.search_audit(job_id)
            snapshot, docs = sanitize_snapshot(result, documents, audit["queries"])
            token, at = secrets.token_urlsafe(32), _now()
            db.execute("INSERT INTO shared_results VALUES(?,?,?,?,?,?)", (token, job_id, row["revision"], at, _dump(snapshot), _dump(docs)))
        return {"token": token, "revision": expected_revision, "created_at": at}

    def get_share(self, token):
        with self._connection() as db:
            row = db.execute("SELECT revision,created_at,result_json,documents_json FROM shared_results WHERE token=?", (token,)).fetchone()
        if not row:
            return None
        return {"revision": row["revision"], "created_at": row["created_at"], "result": _load(row["result_json"], {}), "documents": _load(row["documents_json"], [])}

    def revalidate_result(self, job_id, result, *, expected_revision, note):
        """Replace derived results atomically, retaining the complete prior audit."""
        with self._connection(immediate=True) as db:
            row = db.execute("SELECT status,revision,result_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] == "running":
                raise ValueError("Результат ещё нельзя перепроверить.")
            if row["revision"] != expected_revision:
                raise ValueError("Результат изменился во время проверки; перечитайте его.")
            old = _load(row["result_json"], {})
            revision = row["revision"] + 1
            result = _safe(result)
            result.setdefault("review", {})["revision"] = revision
            audit = {"kind": "source_revalidation", "actor": "Codex", "note": note,
                     "before_revision": row["revision"], "after_revision": revision,
                     "before_result": old, "after_offer_ids": [o.get("id") for o in result.get("offers", [])]}
            db.execute("INSERT INTO steps(id,job_id,created_at,kind,tool,step_json) VALUES(?,?,?,?,?,?)",
                       (uuid.uuid4().hex, job_id, _now(), "source_revalidation", "classify_page", _dump(audit)))
            self._save_result(db, job_id, result)
            db.execute("UPDATE jobs SET revision=?,updated_at=? WHERE id=?", (revision, _now(), job_id))
            return revision

    @staticmethod
    def _period(period):
        now = datetime.now(timezone.utc)
        if period in {"all", "lifetime"}:
            return "all", "all"
        if period == "day":
            return "day", now.strftime("%Y-%m-%d")
        if period == "month":
            return "month", now.strftime("%Y-%m")
        raise ValueError("Неизвестный период квоты.")

    def quota_reserve(self, kind, amount, limit, period="day"):
        if not isinstance(kind, str) or not kind or isinstance(amount, bool) or isinstance(limit, bool) or not isinstance(amount, int) or not isinstance(limit, int) or amount < 0 or limit < 0:
            raise ValueError("Квота задаётся неотрицательными целыми числами.")
        period, key = self._period(period)
        with self._connection(immediate=True) as db:
            db.execute("INSERT OR IGNORE INTO quotas VALUES(?,?,?,0)", (kind, period, key))
            cursor = db.execute("UPDATE quotas SET used=used+? WHERE kind=? AND period=? AND period_key=? AND used+?<=?",
                                (amount, kind, period, key, amount, limit))
            return bool(cursor.rowcount)

    def quota_reserve_many(self, reservations, period="all"):
        """Reserve an entire paid call atomically, or leave every quota intact."""
        if not isinstance(reservations, list) or not reservations:
            raise ValueError("Нужен список резервов квот.")
        seen = set()
        for kind, amount, limit in reservations:
            if (not isinstance(kind, str) or not kind or kind in seen or isinstance(amount, bool)
                    or isinstance(limit, bool) or not isinstance(amount, int) or not isinstance(limit, int)
                    or amount < 0 or limit < 0):
                raise ValueError("Некорректный резерв квот.")
            seen.add(kind)
        period, key = self._period(period)
        with self._connection(immediate=True) as db:
            for kind, amount, limit in reservations:
                row = db.execute("SELECT used FROM quotas WHERE kind=? AND period=? AND period_key=?", (kind, period, key)).fetchone()
                if (row[0] if row else 0) + amount > limit:
                    return False
            for kind, amount, _ in reservations:
                db.execute("INSERT INTO quotas VALUES(?,?,?,?) ON CONFLICT(kind,period,period_key) DO UPDATE SET used=used+excluded.used",
                           (kind, period, key, amount))
            return True

    def quota_usage(self, kind, period="day"):
        period, key = self._period(period)
        with self._connection() as db:
            row = db.execute("SELECT used FROM quotas WHERE kind=? AND period=? AND period_key=?", (kind, period, key)).fetchone()
            return row[0] if row else 0

    def quota_release(self, kind, amount, period="day"):
        """Release only a settled unused reservation; return the amount freed."""
        if not isinstance(kind, str) or not kind or isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("Освобождаемая квота должна быть неотрицательным целым числом.")
        period, key = self._period(period)
        with self._connection(immediate=True) as db:
            row = db.execute("SELECT used FROM quotas WHERE kind=? AND period=? AND period_key=?", (kind, period, key)).fetchone()
            released = min(row[0], amount) if row else 0
            if released:
                db.execute("UPDATE quotas SET used=used-? WHERE kind=? AND period=? AND period_key=?", (released, kind, period, key))
            return released

    def append_review(self, job_id, offer_id, action, reason):
        if action not in {"exclude", "reset"} or not isinstance(reason, str) or len(reason) > 300:
            raise ValueError("Некорректное действие или причина проверки.")
        with self._connection(immediate=True) as db:
            row = db.execute("SELECT revision,result_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("Анализ не найден.")
            result = _load(row["result_json"], {})
            original = result.get("base_result", result)
            if not isinstance(original, dict) or offer_id not in {offer.get("id") for offer in original.get("offers", []) if isinstance(offer, dict)}:
                raise ValueError("Предложение не найдено в исходном результате.")
            revision = row["revision"] + 1
            db.execute("INSERT INTO reviews(job_id,revision,offer_id,action,reason,created_at) VALUES(?,?,?,?,?,?)", (job_id, revision, offer_id, action, _safe(reason.strip()), _now()))
            db.execute("UPDATE jobs SET revision=?,updated_at=? WHERE id=?", (revision, _now(), job_id))
            return revision

    def apply_review(self, job_id, offer_id, action, reason="", *, fields=None, expected_revision=None):
        """Validate, audit and recalculate in one transaction with optimistic CAS."""
        from .review import (RevisionConflict, field_value, rebuild_result, source_lines, validate_fields,
                             validate_acceptances, selected_review_field, shown_suggestion)
        if action not in {"update", "exclude", "reset", "accept_deviation", "confirm_value", "reset_field"} or not isinstance(offer_id, str):
            raise ValueError("Некорректное действие проверки.")
        if action in {"confirm_value", "reset_field"} and isinstance(reason, str) and not reason.strip():
            reason = "Показанное значение подтверждено пользователем" if action == "confirm_value" else "Подтверждение поля отменено пользователем"
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 300:
            raise ValueError("Укажите причину проверки до 300 символов.")
        if expected_revision is not None and (isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0):
            raise ValueError("Некорректная ревизия результата.")
        if action in {"update", "accept_deviation", "confirm_value", "reset_field"} and expected_revision is None:
            raise ValueError("Для сохранения уточнения нужна текущая ревизия.")
        if action not in {"update", "accept_deviation", "confirm_value", "reset_field"} and fields is not None:
            raise ValueError("Поля уточнения допустимы только при сохранении значений.")
        with self._connection(immediate=True) as db:
            row = db.execute("SELECT status,revision,result_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("Анализ не найден.")
            if row["status"] == "running" or row["result_json"] is None:
                raise ValueError("Дождитесь завершения поиска.")
            if expected_revision is not None and expected_revision != row["revision"]:
                raise RevisionConflict(row["revision"])
            result = _load(row["result_json"], {})
            sources = {identifier: (line, baseline, offer) for identifier, line, baseline, offer in source_lines(result)}
            if offer_id not in sources:
                raise ValueError("Предложение не найдено в исходном результате.")
            line, baseline, source = sources[offer_id]
            if not line.get("base_result"):
                raise ValueError("Этот сохранённый результат не поддерживает пересчёт.")
            overrides = {item["offer_id"]: _load(item["confirmations_json"], {}) for item in db.execute(
                "SELECT offer_id,confirmations_json FROM review_overrides WHERE job_id=?", (job_id,))}
            acceptances = {item["offer_id"]: _load(item["acceptances_json"], {}) for item in db.execute(
                "SELECT offer_id,acceptances_json FROM review_acceptances WHERE job_id=?", (job_id,))}
            excluded = set(result.get("review", {}).get("excluded_ids", []))
            for event in db.execute("SELECT offer_id,action FROM reviews WHERE job_id=? ORDER BY revision", (job_id,)):
                if event["action"] == "exclude":
                    excluded.add(event["offer_id"])
                elif event["action"] == "reset":
                    excluded.discard(event["offer_id"])
            revision, at = row["revision"] + 1, _now()
            changes = {}
            if action in {"update", "confirm_value"}:
                supporting_source = None
                if action == "confirm_value":
                    key = selected_review_field(baseline["request"], fields, attributes_only=True)
                    current = next((offer for offer in line.get("offers", []) if offer.get("id") == source["id"]), source)
                    if key in overrides.get(offer_id, {}):
                        raise ValueError("Это поле уже подтверждено. Сначала отмените подтверждение.")
                    if field_value(current, key) is not None:
                        raise ValueError("У поля уже есть значение. Используйте изменение значения.")
                    value, supporting_source = shown_suggestion(baseline["request"], current, key)
                    values = {key: value}
                else:
                    values = validate_fields(baseline["request"], fields)
                confirmations = overrides.setdefault(offer_id, {})
                for key, value in values.items():
                    changes[key] = {"kind": "manual", "value": value, "original_value": field_value(source, key),
                                    "reason": _safe(reason.strip()), "revision": revision, "at": at}
                    if supporting_source is not None:
                        changes[key].update(origin="accepted_suggestion", supporting_source=supporting_source)
                    confirmations[key] = changes[key]
                db.execute("INSERT OR REPLACE INTO review_overrides VALUES(?,?,?)", (job_id, offer_id, _dump(confirmations)))
            elif action == "reset_field":
                key = selected_review_field(baseline["request"], fields)
                confirmations = overrides.get(offer_id, {})
                if key not in confirmations:
                    raise ValueError("У этого поля нет ручного подтверждения.")
                changes[key] = {"removed_confirmation": confirmations.pop(key)}
                if confirmations:
                    db.execute("INSERT OR REPLACE INTO review_overrides VALUES(?,?,?)", (job_id, offer_id, _dump(confirmations)))
                else:
                    overrides.pop(offer_id, None)
                    db.execute("DELETE FROM review_overrides WHERE job_id=? AND offer_id=?", (job_id, offer_id))
            elif action == "accept_deviation":
                values = validate_acceptances(baseline["request"], fields)
                accepted = acceptances.setdefault(offer_id, {})
                for key, value in values.items():
                    changes[key] = {"accepted": value, "reason": _safe(reason.strip()), "revision": revision, "at": at}
                    if value:
                        accepted[key] = changes[key]
                    else:
                        accepted.pop(key, None)
                db.execute("INSERT OR REPLACE INTO review_acceptances VALUES(?,?,?)", (job_id, offer_id, _dump(accepted)))
            elif action == "exclude":
                excluded.add(offer_id)
            else:
                excluded.discard(offer_id)
                overrides.pop(offer_id, None)
                acceptances.pop(offer_id, None)
                db.execute("DELETE FROM review_overrides WHERE job_id=? AND offer_id=?", (job_id, offer_id))
                db.execute("DELETE FROM review_acceptances WHERE job_id=? AND offer_id=?", (job_id, offer_id))
            updated = rebuild_result(result, overrides, excluded, revision, acceptances)
            db.execute("INSERT INTO reviews(job_id,revision,offer_id,action,reason,created_at,fields_json) VALUES(?,?,?,?,?,?,?)",
                       (job_id, revision, offer_id, action, _safe(reason.strip()), at, _dump(changes) if changes else None))
            self._save_result(db, job_id, updated)
            db.execute("UPDATE jobs SET revision=?,updated_at=? WHERE id=?", (revision, at, job_id))
            return revision
