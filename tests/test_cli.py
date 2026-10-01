from __future__ import annotations

import os
import shutil
from argparse import Namespace
from datetime import UTC, datetime

import advanced_used_car_search.cli as cli
import pytest
from advanced_used_car_search.config import load
from advanced_used_car_search.security import canonical_url
from advanced_used_car_search.sources import Result
from advanced_used_car_search.store import Store


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


class LexusCandidateAdapter:
    url = (
        "https://www.lexus.com/lcertified/search-inventory"
        "?link[LcertSearchInventory][setVin]=58AEA1C16NU018844"
    )

    def is_detail(self, url):
        return url == self.url

    def run(self, browser, config, deadline):
        html = """
        <html><body><h1>2022 Lexus ES 300h Luxury</h1>
        <p>$28,900 · 34,500 miles · San Jose, CA 95112 · Clean title.</p>
        </body></html>
        """
        return Result(
            "ok",
            "visible detail overlay read",
            listings=[(self.url, html)],
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


def test_source_diagnostic_does_not_mutate_live_inventory(tmp_path, monkeypatch) -> None:
    database = Store(tmp_path / "listings.sqlite3")
    first_run = database.start(datetime.now(UTC).isoformat())
    database.save(listing(), first_run)
    before = tuple(
        database.db.execute(
            "SELECT observed,last_seen,last_seen_run,missing_runs,stale FROM listings"
        ).fetchone()
    )
    before_counts = tuple(
        database.db.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM runs),"
            "(SELECT COUNT(*) FROM source_events),"
            "(SELECT COUNT(*) FROM observations)"
        ).fetchone()
    )
    database.close()

    config = load(
        enabled_sources=["lexus"],
        run_duration_minutes=1,
        request_delay_seconds=0,
        model_enabled=False,
    )
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "load", lambda *args, **kwargs: config)
    monkeypatch.setattr(cli, "ADAPTERS", {"lexus": LexusCandidateAdapter()})
    monkeypatch.setattr(cli, "Browser", FakeBrowser)
    monkeypatch.setattr(cli, "validate_vin", lambda vin, remote, year: "verified")
    arguments = Namespace(
        config=tmp_path / "config.yaml",
        headless=True,
        duration_minutes=1,
    )
    report = cli.run(arguments, test=True)
    assert report["sources_searched"] == ["lexus"]
    assert report["mode"] == "source diagnostic"
    assert (tmp_path / "reports/diagnostic/latest.json").exists()
    assert not (tmp_path / "reports/latest.json").exists()

    database = Store(tmp_path / "listings.sqlite3")
    after = tuple(
        database.db.execute(
            "SELECT observed,last_seen,last_seen_run,missing_runs,stale FROM listings"
        ).fetchone()
    )
    after_counts = tuple(
        database.db.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM runs),"
            "(SELECT COUNT(*) FROM source_events),"
            "(SELECT COUNT(*) FROM observations)"
        ).fetchone()
    )
    database.close()
    assert after == before
    assert after_counts == before_counts
    diagnostic = Store(tmp_path / "diagnostic.sqlite3")
    assert len(diagnostic.listings()) == 1
    diagnostic.close()
    assert (tmp_path / "evidence/diagnostic/1/lexus-1.html").exists()


def test_source_diagnostic_keeps_trailing_policy_sources_disabled(
    tmp_path, monkeypatch
) -> None:
    cars_adapter = cli.ADAPTERS["cars"]
    config = load(
        enabled_sources=["autotrader", "cars"],
        run_duration_minutes=0,
        request_delay_seconds=0,
        model_enabled=False,
    )
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "load", lambda *args, **kwargs: config)
    monkeypatch.setattr(
        cli,
        "ADAPTERS",
        {"autotrader": EmptyAdapter(), "cars": cars_adapter},
    )
    monkeypatch.setattr(cli, "Browser", FakeBrowser)
    report = cli.run(
        Namespace(config=tmp_path / "config.yaml", headless=True, duration_minutes=0),
        test=True,
    )

    database = Store(tmp_path / "diagnostic.sqlite3")
    events = {event["source"]: event for event in database.events(report["run_id"])}
    database.close()
    assert events["autotrader"]["status"] == "deadline"
    assert events["cars"]["status"] == "disabled"
    assert "automated collection is disabled by default" in events["cars"]["detail"]
    assert "searches=0; retries=0; parsed=0; complete=False" in events["cars"]["detail"]


def test_source_diagnostic_passes_no_browser_to_trailing_disabled_source(
    tmp_path, monkeypatch
) -> None:
    created_browsers = []
    cars_adapter = cli.ADAPTERS["cars"]
    received_browser = object()

    class TrackingBrowser(FakeBrowser):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_browsers.append(self)

    class RecordingAdapter:
        def run(self, browser, config, deadline):
            nonlocal received_browser
            received_browser = browser
            return cars_adapter.run(browser, config, deadline)

    config = load(
        enabled_sources=["autotrader", "cars"],
        run_duration_minutes=0.01,
        request_delay_seconds=0,
        model_enabled=False,
    )
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "load", lambda *args, **kwargs: config)
    monkeypatch.setattr(
        cli,
        "ADAPTERS",
        {"autotrader": EmptyAdapter(), "cars": RecordingAdapter()},
    )
    monkeypatch.setattr(cli, "Browser", TrackingBrowser)
    report = cli.run(
        Namespace(config=tmp_path / "config.yaml", headless=True, duration_minutes=0.01),
        test=True,
    )

    database = Store(tmp_path / "diagnostic.sqlite3")
    events = {event["source"]: event for event in database.events(report["run_id"])}
    database.close()
    assert len(created_browsers) == 1
    assert received_browser is None
    assert events["cars"]["status"] == "disabled"
    assert "searches=0; retries=0; parsed=0; complete=False" in events["cars"]["detail"]


def test_default_source_priority_drives_live_and_diagnostic_dispatch(
    tmp_path, monkeypatch
) -> None:
    config = load(run_duration_minutes=0.01, request_delay_seconds=0, model_enabled=False)
    dealer_adapter = cli.ADAPTERS["dealers"]
    assert config["enabled_sources"][:2] == ["lexus", "dealers"]

    class RecordingAdapter:
        def __init__(self, name, dispatch_order, dealer_browsers):
            self.name = name
            self.dispatch_order = dispatch_order
            self.dealer_browsers = dealer_browsers

        def run(self, browser, run_config, deadline):
            self.dispatch_order.append(self.name)
            if self.name == "dealers":
                self.dealer_browsers.append(browser)
                return dealer_adapter.run(browser, run_config, deadline)
            if self.name in cli.POLICY_RESTRICTED:
                return Result("disabled", cli.POLICY_RESTRICTED[self.name])
            return Result(
                "empty",
                "bounded diagnostic found no detail links",
                completed_searches=1,
                complete=True,
            )

    for test_mode in (False, True):
        root = tmp_path / ("diagnostic" if test_mode else "live")
        root.mkdir()
        dispatch_order = []
        dealer_browsers = []

        monkeypatch.setattr(cli, "ROOT", root)
        monkeypatch.setattr(cli, "load", lambda *args, **kwargs: config)
        monkeypatch.setattr(
            cli,
            "ADAPTERS",
            {
                name: RecordingAdapter(name, dispatch_order, dealer_browsers)
                for name in config["enabled_sources"]
            },
        )
        monkeypatch.setattr(cli, "Browser", FakeBrowser)
        cli.run(
            Namespace(config=root / "config.yaml", headless=True, duration_minutes=0.01),
            test=test_mode,
        )

        assert dispatch_order == config["enabled_sources"]
        assert dispatch_order[:2] == ["lexus", "dealers"]
        assert len(dealer_browsers) == 1
        assert isinstance(dealer_browsers[0], FakeBrowser)


def test_fixture_run_stays_offline_and_isolated_when_model_is_configured(tmp_path, monkeypatch) -> None:
    config = load(request_delay_seconds=0, model_enabled=True)
    shutil.copytree(cli.ROOT / "fixtures", tmp_path / "fixtures")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "load", lambda *args, **kwargs: config)

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline fixtures must not open a browser or call the model")

    monkeypatch.setattr(cli, "Browser", forbidden)
    monkeypatch.setattr(cli.DeepSeekClient, "extract", forbidden)
    report = cli.run(Namespace(config=tmp_path / "config.yaml", model=False), dry=True)
    assert report["mode"] == "dry-run fixtures"
    assert report["discovered"] == 4
    assert report["model_calls"] == 0
    assert (tmp_path / "reports/fixtures/latest.json").exists()
    assert not (tmp_path / "reports/latest.json").exists()
    assert not (tmp_path / "listings.sqlite3").exists()


def test_missing_fixtures_do_not_create_an_empty_success_report(tmp_path, monkeypatch) -> None:
    config = load(model_enabled=False)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "load", lambda *args, **kwargs: config)
    with pytest.raises(ValueError, match="No HTML fixtures"):
        cli.run(Namespace(config=tmp_path / "config.yaml", model=False), dry=True)
    assert not (tmp_path / "fixtures.sqlite3").exists()
    assert not (tmp_path / "reports").exists()


def test_complete_empty_live_source_does_not_age_inventory(tmp_path, monkeypatch) -> None:
    database = Store(tmp_path / "listings.sqlite3")
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

    database = Store(tmp_path / "listings.sqlite3")
    assert database.listings()[0]["missing_runs"] == 0
    database.close()


def test_ingestion_failure_does_not_age_inventory(tmp_path, monkeypatch) -> None:
    database = Store(tmp_path / "listings.sqlite3")
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

    database = Store(tmp_path / "listings.sqlite3")
    assert database.listings()[0]["missing_runs"] == 0
    assert database.events(report["run_id"])[-1]["status"] == "failed"
    database.close()


def test_lexus_query_detail_candidate_is_persisted_with_evidence(tmp_path, monkeypatch) -> None:
    config = load(
        enabled_sources=["lexus"],
        run_duration_minutes=0.01,
        request_delay_seconds=0,
        model_enabled=False,
    )
    adapter = LexusCandidateAdapter()
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "load", lambda *args, **kwargs: config)
    monkeypatch.setattr(cli, "ADAPTERS", {"lexus": adapter})
    monkeypatch.setattr(cli, "Browser", FakeBrowser)
    report = cli.run(
        Namespace(config=tmp_path / "config.yaml", headless=True, duration_minutes=0.01)
    )

    database = Store(tmp_path / "listings.sqlite3")
    saved = database.listings()
    database.close()
    assert report["discovered"] == 1
    assert len(saved) == 1
    assert saved[0]["url"] == canonical_url(adapter.url)
    assert (tmp_path / "evidence/1/lexus-1.html").exists()


def test_generated_output_retention_removes_only_expired_run_artifacts(tmp_path) -> None:
    old_log = tmp_path / "logs/live-run-1.jsonl"
    recent_log = tmp_path / "logs/diagnostic-run-2.jsonl"
    unrelated_log = tmp_path / "logs/manual-run-notes.jsonl"
    old_live_evidence = tmp_path / "evidence/1"
    old_diagnostic_evidence = tmp_path / "evidence/diagnostic/2"
    recent_fixture_evidence = tmp_path / "evidence/fixtures/3"
    report = tmp_path / "reports/latest.json"

    for file in (old_log, recent_log, unrelated_log, report):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("data", encoding="utf-8")
    for directory in (old_live_evidence, old_diagnostic_evidence, recent_fixture_evidence):
        directory.mkdir(parents=True)
        (directory / "page.html").write_text("evidence", encoding="utf-8")

    old_time = 1_000_000.0
    current_time = old_time + 31 * 86_400
    for path in (old_log, unrelated_log, old_live_evidence, old_diagnostic_evidence):
        os.utime(path, (old_time, old_time))
    for path in (recent_log, recent_fixture_evidence):
        os.utime(path, (current_time, current_time))

    result = cli.prune_generated_outputs(tmp_path, retention_days=30, now_epoch=current_time)

    assert result == {"removed": 3, "errors": []}
    assert not old_log.exists()
    assert not old_live_evidence.exists()
    assert not old_diagnostic_evidence.exists()
    assert recent_log.exists()
    assert recent_fixture_evidence.exists()
    assert unrelated_log.exists()
    assert report.exists()
