from __future__ import annotations

import time
from datetime import UTC, datetime

from lexus_hunter.config import ROOT, load
from lexus_hunter.extract import extract, privacy
from lexus_hunter.rank import evaluate, vin_check_digit
from lexus_hunter.sources import ADAPTERS
from lexus_hunter.store import Store

CONFIG = load()


def sample(name: str):
    html = (ROOT / "fixtures" / name).read_text(encoding="utf-8")
    return extract(html, f"https://example.com/vehicle/{name}", "cars", CONFIG, use_model=False)


def test_fixture_matching_and_exclusion() -> None:
    good = evaluate(sample("cars_black.html"), CONFIG, vin_status="missing")
    assert (good["year"], good["price"], good["mileage"]) == (2022, 28_900, 34_500)
    assert good["category"] in {"excellent deal", "strong candidate", "worth watching"}
    assert good["title_confidence"] == "seller claim (unverified)"
    assert evaluate(sample("autotrader_near.html"), CONFIG, vin_status="missing")["category"] == "near match"
    assert evaluate(sample("cargurus_avoid.html"), CONFIG, vin_status="missing")["category"] == "avoid"
    assert evaluate(sample("cars_wrong.html"), CONFIG, vin_status="missing")["category"] == "avoid"


def test_vin_and_privacy() -> None:
    assert vin_check_digit("58AEA1C16NU018844")
    assert not vin_check_digit("1" * 16 + "I")
    assert "[redacted phone]" in privacy("Call 408-555-1234")
    assert "[redacted email]" in privacy("Write x@example.com")


def test_dedup_and_price_history(tmp_path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    first = evaluate(sample("cars_black.html"), CONFIG, vin_status="missing")
    first["found_at"] = datetime.now(UTC).isoformat()
    run = store.start(first["found_at"])
    assert store.save(first, run)["new"] is True
    lowered = dict(first, price=28_000, url=first["url"] + "?utm_source=foo")
    assert store.save(lowered, run)["price_drop"] is True
    assert len(store.listings()) == 1
    assert store.db.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
    store.close()


def test_policy_disabled_requires_no_browser() -> None:
    for name in ("facebook", "cars", "craigslist", "search", "dealers"):
        result = ADAPTERS[name].run(None, CONFIG, time.monotonic() + 10)
        assert result.status == "disabled"
        assert not result.listings and result.completed_searches == 0


def test_only_concrete_detail_links_are_discovered() -> None:
    adapter = ADAPTERS["autotrader"]
    html = (
        '<a href="/cars-for-sale/all-cars/lexus/es-300h">Lexus</a>'
        '<a href="/cars-for-sale/vehicledetails.xhtml?listingId=123">2022 Lexus ES 300h</a>'
    )
    links = adapter.discover(html, "https://www.autotrader.com/cars-for-sale/", CONFIG)
    assert len(links) == 1 and "vehicledetails" in links[0]


class SequenceBrowser:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    def visit(self, url, domains, timeout):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_source_retries_one_transient_failure() -> None:
    browser = SequenceBrowser([RuntimeError("temporary"), "<html><body>No inventory links</body></html>"])
    config = dict(CONFIG, request_delay_seconds=0)
    result = ADAPTERS["autotrader"].run(browser, config, time.monotonic() + 10)
    assert result.status == "empty"
    assert result.completed_searches == 1
    assert result.retries == 1 and browser.calls == 2
    assert result.complete is True


def test_source_does_not_retry_access_restriction() -> None:
    browser = SequenceBrowser([PermissionError("HTTP 403")])
    config = dict(CONFIG, request_delay_seconds=0)
    result = ADAPTERS["autotrader"].run(browser, config, time.monotonic() + 10)
    assert result.status == "blocked"
    assert result.retries == 0 and browser.calls == 1


def test_source_page_budget_invalidates_partial_coverage() -> None:
    search = '<a href="/vehicle/first">first</a><a href="/vehicle/second">second</a>'
    detail = "<html><body>2022 Lexus ES 300h</body></html>"
    browser = SequenceBrowser([search, detail])
    config = dict(CONFIG, request_delay_seconds=0, max_pages_per_source=2)
    result = ADAPTERS["autotrader"].run(browser, config, time.monotonic() + 10)
    assert result.status == "failed"
    assert result.complete is False
    assert browser.calls == 2


class LexusBrowser:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def visit(self, url, domains, timeout):
        self.calls.append("search")
        return "<html><body>Lexus inventory</body></html>"

    def filter_lexus_inventory(self, *, year, radius, timeout):
        self.calls.append(f"filter:{year}:{radius}")
        return '<a href="?link[LcertSearchInventory][setVin]=58AD21B19NU012345">2022 ES 300h VIEW DETAILS</a>'

    def open_lexus_detail(self, url, domains, timeout):
        self.calls.append("detail")
        return (
            "<html><body><h1>2022 Lexus ES 300h</h1>"
            "<p>$28,900 34,500 miles San Jose, CA 95112</p></body></html>"
        )


def test_lexus_adapter_filters_visible_inventory_and_reads_detail() -> None:
    browser = LexusBrowser()
    config = dict(CONFIG, home_zip="95112", request_delay_seconds=0, search_radius_miles=500)
    result = ADAPTERS["lexus"].run(browser, config, time.monotonic() + 10)
    assert result.status == "ok"
    assert result.complete is True
    assert len(result.listings) == 1
    assert browser.calls == ["search", "filter:2022:500", "detail"]


def test_dealer_discovery_requires_target_card_and_concrete_vehicle_identity() -> None:
    config = dict(CONFIG, dealer_urls=["https://dealer.example/used-inventory/"])
    html = """
    <article>
      <h2>2022 Lexus ES 300h Luxury</h2><p>$28,900 · 34,500 miles</p>
      <a href="/inventory/used-2022-lexus-es-300h-58AD21B19NU012345/">View Details</a>
    </article>
    <article>
      <h2>2022 Lexus ES 300h inventory</h2>
      <a href="/used-inventory/?model=es-300h">More results</a>
    </article>
    """
    links = ADAPTERS["dealers"].discover(html, config["dealer_urls"][0], config)
    assert links == ["https://dealer.example/inventory/used-2022-lexus-es-300h-58AD21B19NU012345/"]
    assert ADAPTERS["dealers"].is_detail(links[0])
    assert not ADAPTERS["dealers"].is_detail("https://dealer.example/used-inventory/?model=es-300h")


def test_dealer_detail_without_single_vehicle_evidence_is_rejected() -> None:
    search = """
    <article>
      <h2>2022 Lexus ES 300h Luxury</h2>
      <a href="/inventory/used-2022-lexus-es-300h-58AD21B19NU012345/">View Details</a>
    </article>
    """
    browser = SequenceBrowser([search, "<html><body>2022 Lexus ES 300h inventory</body></html>"])
    config = dict(
        CONFIG,
        dealer_urls=["https://dealer.example/used-inventory/"],
        max_pages_per_source=2,
        request_delay_seconds=0,
    )
    result = ADAPTERS["dealers"].run(browser, config, time.monotonic() + 10)
    assert result.status == "failed"
    assert result.listings == []
