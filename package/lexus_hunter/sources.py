"""Read-only public browsing. No authentication, evasion, forms, contact, or transaction actions."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from .config import SOURCES
from .security import UnsafeUrlError, URLPolicy, canonical_url, domains_from_urls, is_vehicle_detail_url

DOMAINS = {
    "facebook": ("facebook.com",),
    "autotrader": ("autotrader.com",),
    "cars": ("cars.com",),
    "cargurus": ("cargurus.com",),
    "truecar": ("truecar.com",),
    "edmunds": ("edmunds.com",),
    "lexus": ("lexus.com",),
    "dealers": (),
    "craigslist": ("craigslist.org",),
    "search": ("google.com", "bing.com"),
}
POLICY_RESTRICTED = {
    "facebook": "requires an explicitly configured existing authenticated session; login is never attempted",
    "cars": "automated collection is disabled by default; no access-control bypass is attempted",
    "craigslist": "automated collection is disabled by default; no access-control bypass is attempted",
    "search": "automated search-engine querying is disabled by default",
    "dealers": "configure explicit dealer_urls before dealer collection",
}
BLOCKED = re.compile(
    r"captcha|verify you are human|access denied|unusual traffic|sign in to continue|log in to continue|"
    r"automated access|are you a robot|request blocked",
    re.IGNORECASE,
)
TARGET = re.compile(r"\bES\s*300\s*h\b", re.IGNORECASE)
VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE)
PRICE = re.compile(r"\$\s*\d[\d,]*")
MILEAGE = re.compile(r"\b\d[\d,]*\s+(?:miles?|mi)\b", re.IGNORECASE)


@dataclass
class Result:
    status: str
    detail: str
    listings: list[tuple[str, str]] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    completed_searches: int = 0
    retries: int = 0
    search_pages: list[tuple[str, str]] = field(default_factory=list)
    complete: bool = False


class Browser:
    def __init__(self, headless: bool = False, *, max_response_bytes: int = 1_500_000):
        from playwright.sync_api import sync_playwright

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=headless)
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.max_response_bytes = max_response_bytes
        self._active_policy: URLPolicy | None = None
        self.page.route("**/*", self._route)
        self._lexus_year: int | None = None
        self._lexus_radius: int | None = None

    def _route(self, route: Any) -> None:
        request = route.request
        parsed = urlparse(request.url)
        if parsed.scheme in {"data", "blob"}:
            route.continue_()
            return
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            route.abort()
            return
        try:
            host = parsed.hostname.lower().rstrip(".")
            URLPolicy((host,)).validate(request.url)
            if self._active_policy:
                self._active_policy.validate(request.url)
        except UnsafeUrlError:
            route.abort()
            return
        route.continue_()

    def visit(self, url: str, allowed_domains: tuple[str, ...], timeout: int = 18_000) -> str:
        self._active_policy = URLPolicy(allowed_domains)
        safe_url = self._active_policy.validate(url)
        response = self.page.goto(safe_url, wait_until="domcontentloaded", timeout=timeout)
        self.page.wait_for_timeout(700)
        self._active_policy.validate(self.page.url)
        if response and response.status in {401, 403, 429}:
            raise PermissionError(f"HTTP {response.status}: access restriction; no retry")
        if response and response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}")
        return self._checked_html(self.page.content())

    def filter_lexus_inventory(self, *, year: int, radius: int, timeout: int) -> str:
        filter_button = self.page.get_by_role("button", name=re.compile(r"^FILTER"))
        if filter_button.get_attribute("aria-expanded") != "true":
            filter_button.click()
        model = self.page.locator("label").filter(has_text="ES HYBRID")
        if not self.page.locator('input[type="checkbox"][value="ESh"]').is_checked():
            model.click()
        self.page.get_by_role("tab", name="YEAR").click()
        year_input = self.page.locator(f'input[type="checkbox"][value="{year}"]')
        if not year_input.is_checked():
            self.page.locator("label").filter(has_text=str(year)).click()
        self._apply_lexus_filter(
            self.page.get_by_role("button", name="APPLY").last,
            {"model": "ESh", "year": str(year)},
            timeout,
        )

        filter_button = self.page.get_by_role("button", name=re.compile(r"^FILTER"))
        if filter_button.get_attribute("aria-expanded") != "true":
            filter_button.click()
        self.page.get_by_role("tab", name="DISTANCE").click()
        bounded_radius = min(radius, 500)
        distance = next(value for value in (10, 25, 50, 150, 200, 500) if value >= bounded_radius)
        distance_input = self.page.locator(f'input[type="radio"][value="{distance}"]')
        if not distance_input.is_checked():
            self.page.locator("label").filter(has_text=re.compile(rf"^{distance} M")).click()
        self._apply_lexus_filter(
            self.page.get_by_role("button", name="APPLY").last,
            {"model": "ESh", "year": str(year), "radius": str(distance)},
            timeout,
        )
        self._lexus_year = year
        self._lexus_radius = bounded_radius
        return self._checked_html(self.page.content())

    def open_lexus_detail(self, url: str, allowed_domains: tuple[str, ...], timeout: int) -> str:
        policy = URLPolicy(allowed_domains)
        policy.validate(url)
        vin = parse_qs(urlparse(url).query).get("link[LcertSearchInventory][setVin]", [""])[0]
        if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
            raise ValueError("Lexus detail URL does not contain a valid VIN")
        close = self.page.get_by_role("button", name=re.compile(r"^CLOSE OVERLAY$"))
        if close.count() and close.first.is_visible():
            close.first.click()
        link = self.page.locator(f'a[href*="{vin}"]')
        if not link.count():
            raise ValueError("Lexus detail link is no longer present in visible results")
        link.last.click()
        overlay = self.page.locator('[aria-label="VehicleDetails"]')
        overlay.wait_for(state="visible", timeout=timeout)
        self._active_policy = policy
        policy.validate(self.page.url)
        html = f"<html><body>{overlay.inner_html()}</body></html>"
        detail_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        self._validate_lexus_detail_text(detail_text)
        return self._checked_html(html)

    def _validate_lexus_detail_text(self, detail_text: str) -> None:
        if not (
            TARGET.search(detail_text)
            and str(self._lexus_year) in detail_text
            and PRICE.search(detail_text)
            and MILEAGE.search(detail_text)
        ):
            raise ValueError("Lexus detail overlay lacked year, model, price, or mileage evidence")
        distance = re.search(r"([\d,.]+)\s+MILES AWAY", detail_text, re.IGNORECASE)
        if self._lexus_radius is not None:
            if distance is None:
                raise ValueError("Lexus detail overlay lacked distance evidence")
            miles_away = float(distance.group(1).replace(",", ""))
            if miles_away > self._lexus_radius:
                raise ValueError("Lexus detail overlay exceeded the applied search radius")

    def _apply_lexus_filter(
        self,
        button: Any,
        expected_query: dict[str, str],
        timeout: int,
    ) -> None:
        pre_vins = list(
            self.page.locator('a[href*="setVin"]').evaluate_all(
                """links => links
                    .filter(link => link.offsetParent !== null)
                    .map(link => (link.href.match(/[A-HJ-NPR-Z0-9]{17}/) || [])[0])
                    .filter(Boolean)"""
            )
        )

        def matches_inventory_response(response: Any) -> bool:
            parsed = urlparse(response.url)
            if "/rest/lexus/inventorySearch/cpo" not in parsed.path:
                return False
            query = parse_qs(parsed.query)
            return all(query.get(key) == [value] for key, value in expected_query.items())

        with self.page.expect_response(matches_inventory_response, timeout=timeout) as response_info:
            button.click()
        response = response_info.value
        if response.status in {401, 403, 429}:
            raise PermissionError(f"HTTP {response.status}: access restriction; no retry")
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}")
        response_text = self._checked_response_text(response)
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise ValueError("Lexus inventory response was not valid JSON") from exc
        if not isinstance(payload, (dict, list)):
            raise ValueError("Lexus inventory response had an invalid payload")

        response_vins = sorted(set(VIN.findall(response_text)))
        state = {
            "vins": response_vins,
            "previous": sorted(set(pre_vins)),
            "mustChange": set(pre_vins) != set(response_vins),
        }
        self.page.wait_for_function(
            """state => {
                const visibleVins = [...document.querySelectorAll('a[href*="setVin"]')]
                    .filter(link => link.offsetParent !== null)
                    .map(link => (link.href.match(/[A-HJ-NPR-Z0-9]{17}/) || [])[0])
                    .filter(Boolean)
                    .sort();
                if (state.mustChange &&
                    visibleVins.join(',') === [...state.previous].sort().join(',')) {
                    return false;
                }
                if (state.vins.length === 0) {
                    return visibleVins.length === 0 &&
                        document.body.innerText.includes('YOUR SEARCH YIELDED NO RESULTS');
                }
                return visibleVins.length > 0 &&
                    visibleVins.every(vin => state.vins.includes(vin));
            }""",
            arg=state,
            timeout=timeout,
        )

    def _checked_response_text(self, response: Any) -> str:
        declared_size = response.header_value("content-length")
        try:
            size = int(declared_size) if declared_size else None
        except ValueError:
            size = None
        if size is not None and size > self.max_response_bytes:
            raise ValueError("response exceeds configured response-size limit")
        text = response.text()
        if len(text.encode("utf-8")) > self.max_response_bytes:
            raise ValueError("response exceeds configured response-size limit")
        return text

    def _checked_html(self, html: str) -> str:
        if len(html.encode("utf-8")) > self.max_response_bytes:
            raise ValueError("page exceeds configured response-size limit")
        visible = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)[:10_000]
        if BLOCKED.search(visible):
            raise PermissionError("access restriction / authentication / CAPTCHA detected")
        return html

    def close(self) -> None:
        self.context.close()
        self.browser.close()
        self.playwright.stop()


class Adapter:
    name = ""

    def search_urls(self, config: dict[str, Any]) -> list[str]:
        return []

    def allowed_domains(self, config: dict[str, Any]) -> tuple[str, ...]:
        if self.name == "dealers":
            return domains_from_urls(config.get("dealer_urls", []))
        return DOMAINS[self.name]

    def accept(self, url: str, config: dict[str, Any] | None = None) -> bool:
        domain = (urlparse(url).hostname or "").lower().removeprefix("www.")
        allowed = self.allowed_domains(config or {})
        return any(domain == item or domain.endswith("." + item) for item in allowed)

    def discover(self, html: str, base: str, config: dict[str, Any] | None = None) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        identities: set[str] = set()
        for anchor in soup.select("a[href]"):
            href = urljoin(base, str(anchor.get("href") or ""))
            identity = canonical_url(href)
            if (
                identity
                and self.accept(href, config)
                and is_vehicle_detail_url(href)
                and identity != canonical_url(base)
                and identity not in identities
            ):
                links.append(href)
                identities.add(identity)
        return links[:12]

    def is_detail(self, url: str) -> bool:
        return is_vehicle_detail_url(url)

    def visit_search(
        self,
        browser: Browser,
        url: str,
        domains: tuple[str, ...],
        timeout: int,
        config: dict[str, Any],
    ) -> str:
        return browser.visit(url, domains, timeout=timeout)

    def visit_detail(
        self,
        browser: Browser,
        url: str,
        domains: tuple[str, ...],
        timeout: int,
        config: dict[str, Any],
    ) -> str:
        return browser.visit(url, domains, timeout=timeout)

    def run(
        self,
        browser: Browser | None,
        config: dict[str, Any],
        deadline: float,
    ) -> Result:
        if self.name in POLICY_RESTRICTED and (self.name != "dealers" or not config.get("dealer_urls")):
            return Result("disabled", POLICY_RESTRICTED[self.name])
        if browser is None:
            return Result("failed", "browser unavailable")
        domains = self.allowed_domains(config)
        if not domains:
            return Result("disabled", "no configured allowlisted domains")

        searches = self.search_urls(config)
        max_pages = int(config["max_pages_per_source"])
        result = Result("empty", "no search attempted")
        visits = 0
        coverage_complete = len(searches) <= max_pages

        def visit_with_retry(url: str, *, detail: bool = False) -> str:
            nonlocal visits
            if visits >= max_pages:
                raise ValueError("configured per-source page budget exhausted")
            visits += 1
            for attempt in range(2):
                try:
                    timeout = min(18_000, max(1_000, int((deadline - time.monotonic()) * 1_000)))
                    method = self.visit_detail if detail else self.visit_search
                    return method(browser, url, domains, timeout, config)
                except (PermissionError, UnsafeUrlError, ValueError):
                    raise
                except Exception:
                    if attempt == 1 or time.monotonic() + 2 >= deadline:
                        raise
                    result.retries += 1
                    time.sleep(1)
            raise RuntimeError("unreachable retry state")

        visited_details: set[str] = set()
        for search in searches:
            if time.monotonic() >= deadline:
                result.status, result.detail = "deadline", "time limit reached before search"
                coverage_complete = False
                break
            try:
                html = visit_with_retry(search)
                result.completed_searches += 1
                result.search_pages.append((search, html))
                links = self.discover(html, search, config)
                result.status = "empty"
                result.detail = (
                    f"public search loaded; {len(links)} concrete detail links found"
                    if links
                    else "public search loaded but no concrete detail links identified"
                )
                for url in links:
                    identity = canonical_url(url)
                    if identity in visited_details:
                        continue
                    if time.monotonic() >= deadline:
                        result.status, result.detail = "deadline", result.detail + "; time limit reached"
                        coverage_complete = False
                        break
                    try:
                        page_html = visit_with_retry(url, detail=True)
                        visited_details.add(identity)
                        body = BeautifulSoup(page_html, "html.parser").get_text(" ", strip=True)
                        if TARGET.search(body):
                            result.listings.append((url, page_html))
                            result.pages.append(url)
                    except PermissionError as exc:
                        result.status, result.detail = "blocked", str(exc)
                        result.complete = False
                        return result
                    except (UnsafeUrlError, ValueError) as exc:
                        result.status = "failed"
                        result.detail += f"; rejected detail ({exc})"
                        coverage_complete = False
                        break
                    except Exception as exc:
                        result.status = "failed"
                        result.detail += f"; detail unavailable ({type(exc).__name__})"
                        coverage_complete = False
                        break
                    if config["request_delay_seconds"]:
                        time.sleep(
                            min(float(config["request_delay_seconds"]), max(0, deadline - time.monotonic()))
                        )
                if result.status not in {"failed", "deadline"}:
                    result.status = "ok" if result.listings else "empty"
            except PermissionError as exc:
                result.status, result.detail = "blocked", str(exc)
                result.complete = False
                return result
            except UnsafeUrlError as exc:
                result.status, result.detail = "failed", f"unsafe URL rejected: {exc}"
                coverage_complete = False
                break
            except Exception as exc:
                result.status, result.detail = "failed", f"{type(exc).__name__}: {str(exc)[:160]}"
                coverage_complete = False
                break
        result.complete = coverage_complete and result.completed_searches == len(searches)
        return result


class TemplateAdapter(Adapter):
    def __init__(self, name: str):
        self.name = name

    def search_urls(self, config: dict[str, Any]) -> list[str]:
        zip_code = config["home_zip"]
        radius = config.get("search_radius_miles") or 3_000
        urls = {
            "facebook": ["https://www.facebook.com/marketplace/search/?query=2022%20Lexus%20ES%20300h"],
            "autotrader": [
                f"https://www.autotrader.com/cars-for-sale/all-cars/lexus/es-300h/{zip_code}"
                f"?searchRadius={radius}&startYear=2022&endYear=2022"
            ],
            "cars": [
                "https://www.cars.com/shopping/results/?makes[]=lexus&models[]=lexus-es_300h"
                f"&year_min=2022&year_max=2022&zip={zip_code}&maximum_distance={radius}"
            ],
            "cargurus": [f"https://www.cargurus.com/Cars/l-Used-2022-Lexus-ES-Hybrid-c31882?zip={zip_code}"],
            "truecar": [
                "https://www.truecar.com/used-cars-for-sale/listings/lexus/es/year-2022/"
                f"?trim=es-300h&postalCode={zip_code}&searchRadius={radius}"
            ],
            "edmunds": [
                f"https://www.edmunds.com/inventory/srp.html?make=lexus&model=es-300h&year=2022&zip={zip_code}"
            ],
            "lexus": [f"https://www.lexus.com/lcertified/search-inventory?zip={zip_code}"],
            "craigslist": ["https://sfbay.craigslist.org/search/cta?query=2022%20Lexus%20ES%20300h"],
        }
        if self.name == "lexus":
            return urls["lexus"]
        if self.name == "dealers":
            return list(config.get("dealer_urls", []))
        if self.name == "search":
            return []
        return urls.get(self.name, [])


class LexusAdapter(TemplateAdapter):
    def __init__(self) -> None:
        super().__init__("lexus")

    def discover(self, html: str, base: str, config: dict[str, Any] | None = None) -> list[str]:
        links: list[str] = []
        year = str((config or {}).get("target_year", 2022))
        for anchor in BeautifulSoup(html, "html.parser").select("a[href]"):
            href = urljoin(base, str(anchor.get("href") or ""))
            context = anchor
            matches_card = False
            for _ in range(4):
                text = context.get_text(" ", strip=True)
                if TARGET.search(text):
                    matches_card = year in text
                    break
                if context.parent is None:
                    break
                context = context.parent
            if self.is_detail(href) and matches_card and href not in links:
                links.append(href)
        return links[:12]

    def is_detail(self, url: str) -> bool:
        values = parse_qs(urlparse(url).query).get("link[LcertSearchInventory][setVin]", [])
        return bool(values and re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", values[0]))

    def visit_search(
        self,
        browser: Browser,
        url: str,
        domains: tuple[str, ...],
        timeout: int,
        config: dict[str, Any],
    ) -> str:
        browser.visit(url, domains, timeout=timeout)
        configured_radius = int(config.get("search_radius_miles") or 500)
        radius = min(configured_radius, 500)
        return browser.filter_lexus_inventory(
            year=int(config["target_year"]),
            radius=radius,
            timeout=timeout,
        )

    def visit_detail(
        self,
        browser: Browser,
        url: str,
        domains: tuple[str, ...],
        timeout: int,
        config: dict[str, Any],
    ) -> str:
        return browser.open_lexus_detail(url, domains, timeout)


class DealerAdapter(TemplateAdapter):
    def __init__(self) -> None:
        super().__init__("dealers")

    def is_detail(self, url: str) -> bool:
        parsed = urlparse(url)
        return bool(
            VIN.search(url)
            or re.search(r"/(?:vehicle-details|auto)/(?:used|certified)[^/?]+", parsed.path, re.IGNORECASE)
        )

    def discover(self, html: str, base: str, config: dict[str, Any] | None = None) -> list[str]:
        links: list[str] = []
        identities: set[str] = set()
        year = str((config or {}).get("target_year", 2022))
        for anchor in BeautifulSoup(html, "html.parser").select("a[href]"):
            href = urljoin(base, str(anchor.get("href") or ""))
            identity = canonical_url(href)
            context = anchor
            matches_card = False
            for _ in range(4):
                text = context.get_text(" ", strip=True)
                if TARGET.search(text):
                    matches_card = year in text
                    break
                if context.parent is None:
                    break
                context = context.parent
            if (
                identity
                and identity not in identities
                and identity != canonical_url(base)
                and self.accept(href, config)
                and self.is_detail(href)
                and matches_card
            ):
                links.append(href)
                identities.add(identity)
        return links[:12]

    def visit_detail(
        self,
        browser: Browser,
        url: str,
        domains: tuple[str, ...],
        timeout: int,
        config: dict[str, Any],
    ) -> str:
        html = browser.visit(url, domains, timeout=timeout)
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        if not (
            TARGET.search(text)
            and str(config["target_year"]) in text
            and PRICE.search(text)
            and MILEAGE.search(text)
            and (VIN.search(text) or VIN.search(url))
        ):
            raise ValueError("dealer detail lacked one-vehicle VIN, model, year, price, or mileage evidence")
        return html


ADAPTERS = {
    name: (
        LexusAdapter() if name == "lexus" else DealerAdapter() if name == "dealers" else TemplateAdapter(name)
    )
    for name in SOURCES
}
