from __future__ import annotations

import time
from datetime import UTC, datetime

import lexus_hunter.sources as source_module
import pytest
from lexus_hunter.config import ROOT, load
from lexus_hunter.doctor import run_doctor
from lexus_hunter.extract import extract, privacy
from lexus_hunter.rank import evaluate, validate_vin, vin_check_digit
from lexus_hunter.sources import ADAPTERS, Browser, CandidateRejectedError, _lexus_ui_radius
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


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(302, "unavailable"), (401, "blocked"), (403, "blocked"), (429, "blocked")],
)
def test_vin_validation_does_not_follow_redirects_or_access_blocks(
    monkeypatch, status_code: int, expected: str
) -> None:
    options = {}
    calls = []

    class Response:
        def __init__(self):
            self.status_code = status_code

        def raise_for_status(self):
            raise AssertionError("redirects and access blocks must be handled before parsing")

        def json(self):
            raise AssertionError("redirects and access blocks must not be parsed")

    class Client:
        def __init__(self, **kwargs):
            options.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            calls.append(url)
            return Response()

    monkeypatch.setattr("lexus_hunter.rank.httpx.Client", Client)
    assert validate_vin("58AEA1C16NU018844", remote=True, year=2022) == expected
    assert options["follow_redirects"] is False
    assert options["trust_env"] is False
    assert len(calls) == 1


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
    disabled_config = dict(CONFIG, dealer_urls=[])
    for name in ("facebook", "cars", "craigslist", "search", "dealers"):
        result = ADAPTERS[name].run(None, disabled_config, time.monotonic() + 10)
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


@pytest.mark.parametrize(
    "signal",
    [
        "Authentication required",
        "Too many requests",
        "Rate limit exceeded",
        "Paywall",
        "Subscribe to continue",
    ],
)
def test_source_soft_blocks_are_blocked_without_retry(signal: str) -> None:
    checker = object.__new__(Browser)
    checker.max_response_bytes = 10_000

    class SoftBlockBrowser:
        def __init__(self) -> None:
            self.calls = 0

        def visit(self, url, domains, timeout):
            self.calls += 1
            return checker._checked_html(f"<html><body>{signal}</body></html>")

    browser = SoftBlockBrowser()
    config = dict(CONFIG, request_delay_seconds=0)
    result = ADAPTERS["autotrader"].run(browser, config, time.monotonic() + 10)

    assert result.status == "blocked"
    assert result.retries == 0
    assert browser.calls == 1


def test_source_page_budget_invalidates_partial_coverage() -> None:
    search = '<a href="/vehicle/first">first</a><a href="/vehicle/second">second</a>'
    detail = "<html><body>2022 Lexus ES 300h</body></html>"
    browser = SequenceBrowser([search, detail])
    config = dict(CONFIG, request_delay_seconds=0, max_pages_per_source=2)
    result = ADAPTERS["autotrader"].run(browser, config, time.monotonic() + 10)
    assert result.status == "failed"
    assert result.complete is False
    assert browser.calls == 2


def test_rejected_detail_does_not_prevent_later_candidates() -> None:
    search = '<a href="/vehicle/first">first</a><a href="/vehicle/second">second</a>'
    detail = "<html><body>2022 Lexus ES 300h</body></html>"
    browser = SequenceBrowser(
        [
            search,
            CandidateRejectedError("outside requested radius"),
            detail,
        ]
    )
    config = dict(CONFIG, request_delay_seconds=0, max_pages_per_source=3)

    result = ADAPTERS["autotrader"].run(browser, config, time.monotonic() + 10)

    assert result.status == "ok"
    assert result.complete is True
    assert result.listings == [("https://www.autotrader.com/vehicle/second", detail)]
    assert browser.calls == 3


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
    config = dict(CONFIG, home_zip="95112", request_delay_seconds=0, search_radius_miles=250)
    result = ADAPTERS["lexus"].run(browser, config, time.monotonic() + 10)
    assert result.status == "ok"
    assert result.complete is True
    assert len(result.listings) == 1
    assert browser.calls == ["search", "filter:2022:250", "detail"]


def test_lexus_search_shares_one_timeout_budget(monkeypatch) -> None:
    clock = [100.0]
    timeouts = []

    class TimedBrowser:
        def visit(self, url, domains, timeout):
            timeouts.append(timeout)
            clock[0] += 4
            return "<html><body>Lexus inventory</body></html>"

        def filter_lexus_inventory(self, *, year, radius, timeout):
            timeouts.append(timeout)
            return "<html><body>Filtered Lexus inventory</body></html>"

    monkeypatch.setattr(source_module.time, "monotonic", lambda: clock[0])
    source_module.LexusAdapter().visit_search(
        TimedBrowser(),
        "https://www.lexus.com/lcertified/search-inventory?zip=95112",
        ("lexus.com",),
        10_000,
        dict(CONFIG, search_radius_miles=500),
    )

    assert timeouts == [10_000, 6_000]


def test_lexus_shared_timeout_expiry_is_deadline_not_failure(monkeypatch) -> None:
    clock = [0.0]

    class ExpiringBrowser:
        def visit(self, url, domains, timeout):
            clock[0] = 10.0
            return "<html><body>Lexus inventory</body></html>"

        def filter_lexus_inventory(self, *, year, radius, timeout):
            raise AssertionError("filter must not start after the deadline")

    monkeypatch.setattr(source_module.time, "monotonic", lambda: clock[0])
    result = source_module.LexusAdapter().run(
        ExpiringBrowser(),
        dict(CONFIG, request_delay_seconds=0, search_radius_miles=500),
        10.0,
    )

    assert result.status == "deadline"
    assert result.complete is False
    assert result.completed_searches == 0
    assert result.retries == 0



def test_lexus_browser_exception_at_deadline_is_deadline(monkeypatch) -> None:
    clock = [0.0]

    class BrowserTimeout(Exception):
        pass

    class TimedOutBrowser:
        def visit(self, url, domains, timeout):
            clock[0] = 10.0
            raise BrowserTimeout("browser operation timed out")

    monkeypatch.setattr(source_module.time, "monotonic", lambda: clock[0])
    result = source_module.LexusAdapter().run(
        TimedOutBrowser(),
        dict(CONFIG, request_delay_seconds=0, search_radius_miles=500),
        10.0,
    )

    assert result.status == "deadline"
    assert result.complete is False
    assert result.completed_searches == 0
    assert result.retries == 0


def test_lexus_filters_model_year_and_distance_with_one_request() -> None:
    apply_calls = []

    class Control:
        @property
        def last(self):
            return self

        def filter(self, **kwargs):
            return self

        def get_attribute(self, name):
            return "true"

        def is_checked(self):
            return False

        def click(self):
            return None

    class FilterPage:
        def get_by_role(self, role, name):
            return Control()

        def locator(self, selector):
            return Control()

        def content(self):
            return "<html><body>Filtered Lexus inventory</body></html>"

    class FilterBrowser:
        page = FilterPage()
        _lexus_year = None
        _lexus_radius = None

        def _apply_lexus_filter(self, button, expected_query, timeout):
            apply_calls.append((expected_query, timeout))

        def _checked_html(self, html):
            return html

    source_module.Browser.filter_lexus_inventory(
        FilterBrowser(),
        year=2022,
        radius=500,
        timeout=10_000,
    )

    assert apply_calls == [
        ({"model": "ESh", "year": "2022", "radius": "500"}, 10_000)
    ]


def test_lexus_deadline_after_search_is_incomplete_and_unparsed(monkeypatch) -> None:
    clock = [0.0]

    class DeadlineBrowser:
        def visit(self, url, domains, timeout):
            return "<html><body>Lexus inventory</body></html>"

        def filter_lexus_inventory(self, *, year, radius, timeout):
            clock[0] = 10.0
            return (
                '<a href="?link[LcertSearchInventory][setVin]=58AD21B19NU012345">'
                "2022 ES 300h VIEW DETAILS</a>"
            )

        def open_lexus_detail(self, url, domains, timeout):
            raise AssertionError("detail must not start after the deadline")

    monkeypatch.setattr(source_module.time, "monotonic", lambda: clock[0])
    result = source_module.LexusAdapter().run(
        DeadlineBrowser(),
        dict(CONFIG, request_delay_seconds=0, search_radius_miles=500),
        10.0,
    )

    assert result.status == "deadline"
    assert result.complete is False
    assert result.completed_searches == 1
    assert result.listings == []


@pytest.mark.parametrize(("requested", "selected"), [(250, 500), (501, 500)])
def test_lexus_ui_radius_uses_ceiling_capped_at_500(requested: int, selected: int) -> None:
    assert _lexus_ui_radius(requested) == selected


def test_lexus_reports_incomplete_coverage_above_500_miles() -> None:
    browser = LexusBrowser()
    config = dict(CONFIG, request_delay_seconds=0, search_radius_miles=501)
    result = ADAPTERS["lexus"].run(browser, config, time.monotonic() + 10)
    assert result.status == "ok"
    assert result.complete is False
    assert "capped at 500 miles for requested 501 miles; coverage incomplete" in result.detail
    assert browser.calls == ["search", "filter:2022:501", "detail"]


def test_lexus_detail_requires_distance_for_exact_radius_enforcement() -> None:
    browser = object.__new__(Browser)
    browser._lexus_year = 2022
    browser._lexus_radius = 250
    with pytest.raises(ValueError, match="lacked distance evidence"):
        browser._validate_lexus_detail_text("2022 ES 300h $28,900 34,500 miles")
    with pytest.raises(ValueError, match="exceeded the applied search radius"):
        browser._validate_lexus_detail_text("2022 ES 300h $28,900 34,500 miles Location 251 MILES AWAY")


def test_lexus_inventory_response_size_is_checked_before_body_read() -> None:
    class OversizedResponse:
        text_called = False

        def header_value(self, name):
            assert name == "content-length"
            return "11"

        def text(self):
            self.text_called = True
            return "x" * 11

    browser = object.__new__(Browser)
    browser.max_response_bytes = 10
    response = OversizedResponse()
    with pytest.raises(ValueError, match="response-size limit"):
        browser._checked_response_text(response)
    assert response.text_called is False


def test_lexus_inventory_response_checks_actual_body_size() -> None:
    class ChunkedOversizedResponse:
        def header_value(self, name):
            assert name == "content-length"
            return None

        def text(self):
            return "£" * 6

    browser = object.__new__(Browser)
    browser.max_response_bytes = 10
    with pytest.raises(ValueError, match="response-size limit"):
        browser._checked_response_text(ChunkedOversizedResponse())


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
    assert result.status == "empty"
    assert result.complete is True
    assert result.listings == []


def test_doctor_treats_configured_dealers_as_directly_testable(tmp_path, monkeypatch) -> None:
    class LaunchableBrowser:
        def __init__(self, *args, **kwargs):
            return None

        def close(self):
            return None

    monkeypatch.setattr("lexus_hunter.doctor.Browser", LaunchableBrowser)
    checks = run_doctor(
        load(
            enabled_sources=["dealers"],
            dealer_urls=["https://dealer.example/inventory"],
        ),
        tmp_path,
    )
    sources = next(check for check in checks if check.name == "sources")

    assert sources.status == "ok"
    assert "directly testable=dealers" in sources.detail
