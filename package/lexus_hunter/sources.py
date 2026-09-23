"""Read-only public browsing. No authentication, evasion, forms, contact, or transaction actions."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

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
        html = self.page.content()
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

        def visit_with_retry(url: str) -> str:
            nonlocal visits
            if visits >= max_pages:
                raise ValueError("configured per-source page budget exhausted")
            visits += 1
            for attempt in range(2):
                try:
                    return browser.visit(
                        url,
                        domains,
                        timeout=min(18_000, max(1_000, int((deadline - time.monotonic()) * 1_000))),
                    )
                except (PermissionError, UnsafeUrlError, ValueError):
                    raise
                except Exception:
                    if attempt == 1 or time.monotonic() + 2 >= deadline:
                        raise
                    result.retries += 1
                    time.sleep(1)
            raise RuntimeError("unreachable retry state")

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
                    if time.monotonic() >= deadline:
                        result.status, result.detail = "deadline", result.detail + "; time limit reached"
                        coverage_complete = False
                        break
                    try:
                        page_html = visit_with_retry(url)
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
        if self.name == "dealers":
            return list(config.get("dealer_urls", []))
        if self.name == "search":
            return []
        return urls.get(self.name, [])


ADAPTERS = {name: TemplateAdapter(name) for name in SOURCES}
