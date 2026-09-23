from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .security import canonical_url

SCHEMA_VERSION = 3
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, started TEXT NOT NULL, finished TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS listings(
  id INTEGER PRIMARY KEY,
  identity TEXT UNIQUE,
  url TEXT,
  source TEXT,
  vin TEXT,
  seller TEXT,
  price INTEGER,
  mileage INTEGER,
  observed TEXT,
  data TEXT,
  first_seen TEXT,
  last_seen TEXT,
  last_seen_run INTEGER,
  missing_runs INTEGER NOT NULL DEFAULT 0,
  relisted_count INTEGER NOT NULL DEFAULT 0,
  reappeared_at TEXT,
  stale INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS observations(
  id INTEGER PRIMARY KEY,
  listing_id INTEGER,
  run_id INTEGER,
  observed TEXT,
  price INTEGER,
  data TEXT,
  FOREIGN KEY(listing_id) REFERENCES listings(id),
  FOREIGN KEY(run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS listing_sources(
  listing_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  last_seen_run INTEGER NOT NULL,
  missing_runs INTEGER NOT NULL DEFAULT 0,
  stale INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(listing_id, source),
  FOREIGN KEY(listing_id) REFERENCES listings(id),
  FOREIGN KEY(last_seen_run) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS source_events(
  id INTEGER PRIMARY KEY, run_id INTEGER, source TEXT, status TEXT, detail TEXT, time TEXT,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);
"""


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(listings)")}
        migrations = {
            "first_seen": "ALTER TABLE listings ADD COLUMN first_seen TEXT",
            "last_seen": "ALTER TABLE listings ADD COLUMN last_seen TEXT",
            "last_seen_run": "ALTER TABLE listings ADD COLUMN last_seen_run INTEGER",
            "missing_runs": "ALTER TABLE listings ADD COLUMN missing_runs INTEGER NOT NULL DEFAULT 0",
            "relisted_count": "ALTER TABLE listings ADD COLUMN relisted_count INTEGER NOT NULL DEFAULT 0",
            "reappeared_at": "ALTER TABLE listings ADD COLUMN reappeared_at TEXT",
            "stale": "ALTER TABLE listings ADD COLUMN stale INTEGER NOT NULL DEFAULT 0",
        }
        for column, statement in migrations.items():
            if column not in columns:
                self.db.execute(statement)
        self.db.execute("DELETE FROM schema_meta")
        self.db.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
        self.db.execute(
            "UPDATE listings SET first_seen=COALESCE(first_seen,observed), "
            "last_seen=COALESCE(last_seen,observed)"
        )
        self.db.execute(
            "INSERT OR IGNORE INTO listing_sources("
            "listing_id,source,first_seen,last_seen,last_seen_run,missing_runs,stale"
            ") SELECT id,source,COALESCE(first_seen,observed),COALESCE(last_seen,observed),"
            "COALESCE(last_seen_run,0),missing_runs,stale FROM listings "
            "WHERE source IS NOT NULL AND source != '' AND last_seen_run IS NOT NULL"
        )
        self.db.commit()

    def schema_version(self) -> int:
        return int(self.db.execute("SELECT version FROM schema_meta").fetchone()[0])

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def start(self, started: str) -> int:
        cursor = self.db.execute("INSERT INTO runs(started) VALUES (?)", (started,))
        self.db.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("run insert did not return an id")
        return cursor.lastrowid

    def event(self, run_id: int, source: str, status: str, detail: str, observed: str) -> None:
        self.db.execute(
            "INSERT INTO source_events(run_id,source,status,detail,time) VALUES (?,?,?,?,?)",
            (run_id, source, status, detail, observed),
        )
        self.db.commit()

    def identity(self, item: dict[str, Any]) -> str:
        if item.get("vin"):
            row = self.db.execute("SELECT identity FROM listings WHERE vin=?", (item["vin"],)).fetchone()
            if row:
                return str(row["identity"])
            return "vin:" + str(item["vin"])
        url = canonical_url(item.get("url") or "")
        row = self.db.execute("SELECT identity FROM listings WHERE url=?", (url,)).fetchone()
        if row:
            return str(row["identity"])
        for row in self.db.execute('SELECT identity,data FROM listings WHERE vin IS NULL OR vin=""'):
            other = json.loads(row["data"])
            same_attributes = (
                item.get("seller_name")
                and item.get("seller_name") == other.get("seller_name")
                and item.get("price") == other.get("price")
                and item.get("mileage") == other.get("mileage")
                and item.get("year") == other.get("year")
            )
            if not same_attributes:
                continue
            shared_photos = set(item.get("photo_hashes") or []) & set(other.get("photo_hashes") or [])
            similarity = SequenceMatcher(
                None, item.get("description") or "", other.get("description") or ""
            ).ratio()
            if shared_photos or similarity > 0.88:
                return str(row["identity"])
        return "url:" + url

    def save(self, item: dict[str, Any], run_id: int) -> dict[str, Any]:
        identity = self.identity(item)
        old = self.db.execute("SELECT * FROM listings WHERE identity=?", (identity,)).fetchone()
        is_new = old is None
        price_drop = bool(
            old
            and item.get("price") is not None
            and old["price"] is not None
            and item["price"] < old["price"]
        )
        presence = (
            self.db.execute(
                "SELECT missing_runs FROM listing_sources WHERE listing_id=? AND source=?",
                (old["id"], item["source"]),
            ).fetchone()
            if old
            else None
        )
        relisted = bool(presence and int(presence["missing_runs"] or 0) > 0)
        observed = str(item["found_at"])
        first_seen = observed if old is None else str(old["first_seen"] or old["observed"] or observed)
        relisted_count = 0 if old is None else int(old["relisted_count"] or 0) + (1 if relisted else 0)
        existing_sources = (
            {
                str(row["source"])
                for row in self.db.execute(
                    "SELECT source FROM listing_sources WHERE listing_id=?",
                    (old["id"],),
                )
            }
            if old
            else set()
        )
        item.update(
            {
                "identity": identity,
                "first_seen": first_seen,
                "last_seen": observed,
                "missing_runs": 0,
                "relisted": relisted,
                "relisted_count": relisted_count,
                "reappeared_at": observed if relisted else (old["reappeared_at"] if old else None),
                "stale": False,
                "sources": sorted(existing_sources | {str(item["source"])}),
                "price_drop": (int(old["price"]) - int(item["price"])) if price_drop else None,
            }
        )
        data = json.dumps(item, sort_keys=True)
        if old:
            listing_id = int(old["id"])
            self.db.execute(
                "UPDATE listings SET url=?,source=?,vin=?,seller=?,price=?,mileage=?,"
                "observed=?,data=?,first_seen=?,last_seen=?,last_seen_run=?,missing_runs=0,"
                "relisted_count=?,reappeared_at=?,stale=0 WHERE id=?",
                (
                    item["url"],
                    item["source"],
                    item.get("vin"),
                    item.get("seller_name"),
                    item.get("price"),
                    item.get("mileage"),
                    observed,
                    data,
                    first_seen,
                    observed,
                    run_id,
                    relisted_count,
                    item["reappeared_at"],
                    listing_id,
                ),
            )
        else:
            cursor = self.db.execute(
                "INSERT INTO listings(identity,url,source,vin,seller,price,mileage,observed,"
                "data,first_seen,last_seen,last_seen_run,missing_runs,relisted_count,"
                "reappeared_at,stale) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,NULL,0)",
                (
                    identity,
                    item["url"],
                    item["source"],
                    item.get("vin"),
                    item.get("seller_name"),
                    item.get("price"),
                    item.get("mileage"),
                    observed,
                    data,
                    first_seen,
                    observed,
                    run_id,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("listing insert did not return an id")
            listing_id = cursor.lastrowid
        self.db.execute(
            "INSERT INTO listing_sources("
            "listing_id,source,first_seen,last_seen,last_seen_run,missing_runs,stale"
            ") VALUES (?,?,?,?,?,0,0) "
            "ON CONFLICT(listing_id,source) DO UPDATE SET "
            "last_seen=excluded.last_seen,last_seen_run=excluded.last_seen_run,missing_runs=0,stale=0",
            (listing_id, item["source"], observed, observed, run_id),
        )
        self.db.execute(
            "INSERT INTO observations(listing_id,run_id,observed,price,data) VALUES (?,?,?,?,?)",
            (listing_id, run_id, observed, item.get("price"), data),
        )
        self.db.commit()
        return {"new": is_new, "price_drop": price_drop, "relisted": relisted, "identity": identity}

    def finalize_inventory(self, run_id: int, stale_after_days: int, authoritative_sources: set[str]) -> None:
        if not authoritative_sources:
            return
        now = datetime.now(UTC)
        placeholders = ",".join("?" for _ in authoritative_sources)
        rows = list(
            self.db.execute(
                f"SELECT listing_id,source,last_seen,missing_runs FROM listing_sources "
                f"WHERE last_seen_run IS NOT ? AND source IN ({placeholders})",
                (run_id, *sorted(authoritative_sources)),
            )
        )
        touched: set[int] = set()
        for row in rows:
            missing_runs = int(row["missing_runs"] or 0) + 1
            try:
                last_seen = datetime.fromisoformat(str(row["last_seen"]).replace("Z", "+00:00"))
                age_days = (now - last_seen).days
            except (TypeError, ValueError):
                age_days = stale_after_days
            stale = missing_runs >= 2 or age_days >= stale_after_days
            listing_id = int(row["listing_id"])
            touched.add(listing_id)
            self.db.execute(
                "UPDATE listing_sources SET missing_runs=?,stale=? WHERE listing_id=? AND source=?",
                (missing_runs, int(stale), listing_id, row["source"]),
            )
        for listing_id in touched:
            aggregate = self.db.execute(
                "SELECT MIN(missing_runs) AS missing_runs, MIN(stale) AS stale "
                "FROM listing_sources WHERE listing_id=?",
                (listing_id,),
            ).fetchone()
            listing = self.db.execute("SELECT data FROM listings WHERE id=?", (listing_id,)).fetchone()
            if not aggregate or not listing:
                continue
            missing_runs = int(aggregate["missing_runs"] or 0)
            stale = bool(aggregate["stale"])
            data = json.loads(listing["data"])
            data["missing_runs"] = missing_runs
            data["stale"] = stale
            self.db.execute(
                "UPDATE listings SET missing_runs=?,stale=?,data=? WHERE id=?",
                (missing_runs, int(stale), json.dumps(data, sort_keys=True), listing_id),
            )
        self.db.commit()

    def listings(self) -> list[dict[str, Any]]:
        return [
            json.loads(row["data"])
            for row in self.db.execute("SELECT data FROM listings ORDER BY observed DESC")
        ]

    def query(self, *, source: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if source:
            rows = self.db.execute(
                "SELECT l.data FROM listings l JOIN listing_sources s ON s.listing_id=l.id "
                "WHERE s.source=? ORDER BY l.observed DESC LIMIT ?",
                (source, limit),
            )
        else:
            rows = self.db.execute("SELECT data FROM listings ORDER BY observed DESC LIMIT ?", (limit,))
        return [json.loads(row["data"]) for row in rows]

    def events(self, run_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT source,status,detail,time FROM source_events WHERE run_id=? ORDER BY id", (run_id,)
            )
        ]

    def latest(self) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM runs WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def finish(self, run_id: int, finished: str, summary: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE runs SET finished=?,summary=? WHERE id=?",
            (finished, json.dumps(summary, sort_keys=True), run_id),
        )
        self.db.commit()
