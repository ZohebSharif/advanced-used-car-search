from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from lexus_hunter.store import SCHEMA_VERSION, Store


def item(source: str = "autotrader", *, observed: str | None = None, price: int = 29_000):
    return {
        "url": f"https://example.com/vehicle/{source}-1",
        "source": source,
        "vin": "",
        "seller_name": f"{source} Motors",
        "price": price,
        "mileage": 30_000,
        "year": 2022,
        "description": f"2022 Lexus ES 300h Luxury at {source} in black with palomino interior",
        "photo_hashes": [],
        "found_at": observed or datetime.now(UTC).isoformat(),
    }


def test_relisting_lifecycle_and_price_history(tmp_path) -> None:
    store = Store(tmp_path / "lifecycle.sqlite3")
    first_time = datetime.now(UTC)
    run1 = store.start(first_time.isoformat())
    assert store.save(item(observed=first_time.isoformat()), run1)["new"] is True
    store.finalize_inventory(run1, 30, {"autotrader"})

    run2 = store.start((first_time + timedelta(minutes=1)).isoformat())
    store.finalize_inventory(run2, 30, {"autotrader"})
    assert store.listings()[0]["missing_runs"] == 1

    run3 = store.start((first_time + timedelta(minutes=2)).isoformat())
    change = store.save(item(observed=(first_time + timedelta(minutes=2)).isoformat(), price=28_000), run3)
    store.finalize_inventory(run3, 30, {"autotrader"})
    listing = store.listings()[0]
    assert change["relisted"] is True and change["price_drop"] is True
    assert listing["missing_runs"] == 0
    assert listing["relisted"] is True and listing["relisted_count"] == 1
    assert listing["reappeared_at"] is not None
    assert store.db.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
    store.close()


def test_only_authoritatively_searched_sources_age(tmp_path) -> None:
    store = Store(tmp_path / "authoritative.sqlite3")
    run1 = store.start(datetime.now(UTC).isoformat())
    store.save(item("autotrader"), run1)
    store.save(item("cargurus"), run1)

    run2 = store.start(datetime.now(UTC).isoformat())
    store.finalize_inventory(run2, 30, {"autotrader"})
    listings = {entry["source"]: entry for entry in store.listings()}
    assert listings["autotrader"]["missing_runs"] == 1
    assert listings["cargurus"]["missing_runs"] == 0
    store.close()


def test_cross_source_duplicate_ages_only_when_every_presence_is_stale(tmp_path) -> None:
    store = Store(tmp_path / "cross-source.sqlite3")
    run1 = store.start(datetime.now(UTC).isoformat())
    autotrader = item("autotrader")
    cargurus = dict(
        autotrader,
        url="https://example.com/vehicle/cargurus-1",
        source="cargurus",
    )
    store.save(autotrader, run1)
    store.save(cargurus, run1)
    assert len(store.listings()) == 1
    assert store.listings()[0]["sources"] == ["autotrader", "cargurus"]

    run2 = store.start(datetime.now(UTC).isoformat())
    store.finalize_inventory(run2, 30, {"autotrader"})
    listing = store.listings()[0]
    assert listing["missing_runs"] == 0
    assert listing["stale"] is False
    assert len(store.query(source="autotrader")) == 1
    assert len(store.query(source="cargurus")) == 1
    store.close()


def test_rediscovered_detail_url_never_becomes_missing(tmp_path) -> None:
    store = Store(tmp_path / "rediscovered.sqlite3")
    run1 = store.start(datetime.now(UTC).isoformat())
    store.save(item(), run1)
    run2 = store.start(datetime.now(UTC).isoformat())
    change = store.save(item(), run2)
    store.finalize_inventory(run2, 30, {"autotrader"})
    listing = store.listings()[0]
    assert listing["missing_runs"] == 0
    assert change["relisted"] is False
    store.close()


def test_two_authoritative_misses_mark_stale(tmp_path) -> None:
    store = Store(tmp_path / "stale.sqlite3")
    run1 = store.start(datetime.now(UTC).isoformat())
    store.save(item(), run1)
    run2 = store.start(datetime.now(UTC).isoformat())
    store.finalize_inventory(run2, 30, {"autotrader"})
    run3 = store.start(datetime.now(UTC).isoformat())
    store.finalize_inventory(run3, 30, {"autotrader"})
    assert store.listings()[0]["stale"] is True
    store.close()


def test_existing_database_is_migrated_in_place(tmp_path) -> None:
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE listings(
          id INTEGER PRIMARY KEY, identity TEXT UNIQUE, url TEXT, source TEXT, vin TEXT,
          seller TEXT, price INTEGER, mileage INTEGER, observed TEXT, data TEXT
        );
        """
    )
    data = item(observed="2026-01-01T00:00:00+00:00")
    connection.execute(
        "INSERT INTO listings(identity,url,source,observed,data) VALUES (?,?,?,?,?)",
        (
            "url:https://example.com/vehicle/autotrader-1",
            data["url"],
            "autotrader",
            data["found_at"],
            json.dumps(data),
        ),
    )
    connection.commit()
    connection.close()

    store = Store(path)
    migrated = store.listings()[0]
    assert store.schema_version() == SCHEMA_VERSION
    row = store.db.execute("SELECT first_seen,last_seen,missing_runs FROM listings").fetchone()
    assert row["first_seen"] == data["found_at"] == row["last_seen"]
    assert row["missing_runs"] == 0
    assert migrated["url"] == data["url"]
    store.close()
