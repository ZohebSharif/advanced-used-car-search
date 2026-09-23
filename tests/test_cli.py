from __future__ import annotations

from argparse import Namespace
from datetime import UTC, datetime

import lexus_hunter.cli as cli
from lexus_hunter.config import load
from lexus_hunter.sources import Result
from lexus_hunter.store import Store


class EmptyAdapter:
    def run(self, browser, config, deadline):
        return Result(
            "empty",
            "bounded diagnostic found no detail links",
            completed_searches=1,
            complete=True,
        )


class BrokenCandidateAdapter:
    def run(self, browser, config, deadline):
        return Result(
            "ok",
            "candidate fetched",
            listings=[("https://example.com/not-a-detail", "<html></html>")],
            completed_searches=1,
            complete=True,
        )


class FakeBrowser:
    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass


def listing():
    return {
        "url": "https://example.com/vehicle/1",
        "source": "autotrader",
        "vin": "",
        "seller_name": "Example Motors",
        "price": 29_000,
        "mileage": 30_000,
        "year": 2022,
        "description": "2022 Lexus ES 300h",
        "photo_hashes": [],
        "found_at": datetime.now(UTC).isoformat(),
    }


def test_source_diagnostic_does_not_age_inventory(tmp_path, monkeypatch) -> None:
    database = Store(tmp_path / "hunter.sqlite3")
    first_run = database.start(datetime.now(UTC).isoformat())
    database.save(listing(), first_run)
    database.close()

    config = load(
        enabled_sources=["autotrader"],
        run_duration_minutes=0.01,
        request_delay_seconds=0,
        model_enabled=False,
    )
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "load", lambda *args, **kwargs: config)
    monkeypatch.setattr(cli, "ADAPTERS", {"autotrader": EmptyAdapter()})
    monkeypatch.setattr(cli, "Browser", FakeBrowser)
    arguments = Namespace(
        config=tmp_path / "config.yaml",
        headless=True,
        duration_minutes=0.01,
    )
    report = cli.run(arguments, test=True)
    assert report["sources_searched"] == ["autotrader"]
    assert report["mode"] == "source diagnostic"
    assert (tmp_path / "reports/diagnostic/latest.json").exists()
    assert not (tmp_path / "reports/latest.json").exists()

    database = Store(tmp_path / "hunter.sqlite3")
    assert database.listings()[0]["missing_runs"] == 0
    database.close()


def test_fixture_run_writes_only_fixture_report(tmp_path, monkeypatch) -> None:
    config = load(request_delay_seconds=0, model_enabled=False)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "load", lambda *args, **kwargs: config)
    report = cli.run(Namespace(config=tmp_path / "config.yaml", model=False), dry=True)
    assert report["mode"] == "dry-run fixtures"
    assert (tmp_path / "reports/fixtures/latest.json").exists()
    assert not (tmp_path / "reports/latest.json").exists()


def test_complete_empty_live_source_does_not_age_inventory(tmp_path, monkeypatch) -> None:
    database = Store(tmp_path / "hunter.sqlite3")
    first_run = database.start(datetime.now(UTC).isoformat())
    database.save(listing(), first_run)
    database.close()

    config = load(
        enabled_sources=["autotrader"],
        run_duration_minutes=0.01,
        request_delay_seconds=0,
        model_enabled=False,
    )
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "load", lambda *args, **kwargs: config)
    monkeypatch.setattr(cli, "ADAPTERS", {"autotrader": EmptyAdapter()})
    monkeypatch.setattr(cli, "Browser", FakeBrowser)
    report = cli.run(
        Namespace(config=tmp_path / "config.yaml", headless=True, duration_minutes=0.01)
    )
    assert report["mode"] == "live browser"
    assert report["sources_searched"] == ["autotrader"]

    database = Store(tmp_path / "hunter.sqlite3")
    assert database.listings()[0]["missing_runs"] == 0
    database.close()


def test_ingestion_failure_does_not_age_inventory(tmp_path, monkeypatch) -> None:
    database = Store(tmp_path / "hunter.sqlite3")
    first_run = database.start(datetime.now(UTC).isoformat())
    database.save(listing(), first_run)
    database.close()

    config = load(
        enabled_sources=["autotrader"],
        run_duration_minutes=0.01,
        request_delay_seconds=0,
        model_enabled=False,
    )
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "load", lambda *args, **kwargs: config)
    monkeypatch.setattr(cli, "ADAPTERS", {"autotrader": BrokenCandidateAdapter()})
    monkeypatch.setattr(cli, "Browser", FakeBrowser)
    report = cli.run(
        Namespace(config=tmp_path / "config.yaml", headless=True, duration_minutes=0.01)
    )
    assert report["sources_searched"] == []

    database = Store(tmp_path / "hunter.sqlite3")
    assert database.listings()[0]["missing_runs"] == 0
    assert database.events(report["run_id"])[-1]["status"] == "failed"
    database.close()
