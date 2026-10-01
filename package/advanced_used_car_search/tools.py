from __future__ import annotations

import ipaddress
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .extract import extract, privacy
from .model import ModelBudget
from .rank import validate_vin
from .report import build, write
from .security import UnsafeUrlError, URLPolicy, domains_from_urls
from .sources import ADAPTERS, BLOCKED, DOMAINS, Browser
from .store import Store


class ToolLimitError(RuntimeError):
    pass


class AgentTools:
    """Small read-only capability boundary. No form, auth, contact, or transaction methods exist."""

    def __init__(
        self,
        config: dict[str, Any],
        store: Store,
        evidence_dir: Path,
        *,
        browser: Browser | None = None,
        model_budget: ModelBudget | None = None,
        run_id: int | None = None,
    ):
        self.config = config
        self.store = store
        self.evidence_dir = evidence_dir
        self.browser = browser
        self.model_budget = model_budget
        self.run_id = run_id
        self.page_calls = 0
        self.last_request_at = 0.0

    def _domains(self, source: str) -> tuple[str, ...]:
        if source not in ADAPTERS:
            raise ValueError(f"unknown source: {source}")
        if source == "dealers":
            domains = domains_from_urls(self.config.get("dealer_urls", []))
        else:
            domains = DOMAINS[source]
        if not domains:
            raise ValueError(f"source has no configured allowlisted domains: {source}")
        return domains

    def _policy(self, source: str) -> URLPolicy:
        return URLPolicy(self._domains(source))

    def _consume_page(self) -> None:
        if self.page_calls >= int(self.config["max_pages_per_source"]):
            raise ToolLimitError("configured page limit reached")
        delay = float(self.config["request_delay_seconds"])
        wait = delay - (time.monotonic() - self.last_request_at)
        if wait > 0:
            time.sleep(wait)
        self.page_calls += 1
        self.last_request_at = time.monotonic()

    def fetch_public_page(self, url: str, source: str) -> str:
        policy = self._policy(source)
        current = policy.validate(url)
        self._consume_page()
        timeout = min(float(self.config["model_timeout_seconds"]), 30.0)
        max_bytes = int(self.config["max_response_bytes"])
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "advanced-used-car-search/0.2"},
            trust_env=False,
        ) as client:
            for _ in range(4):
                with client.stream("GET", current) as response:
                    stream = response.extensions.get("network_stream")
                    peer = stream.get_extra_info("server_addr") if stream else None
                    peer_address = peer[0] if isinstance(peer, tuple) and peer else peer
                    try:
                        peer_ip = ipaddress.ip_address(str(peer_address).split("%")[0])
                    except ValueError as exc:
                        raise UnsafeUrlError("could not verify response peer address") from exc
                    if not peer_ip.is_global:
                        raise UnsafeUrlError("response connected to a non-public address")
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise RuntimeError("redirect response had no Location header")
                        current = policy.validate(urljoin(str(response.url), location))
                        continue
                    if response.status_code in {401, 403, 429}:
                        raise PermissionError(f"HTTP {response.status_code}: access restriction")
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise ToolLimitError("response exceeds configured byte limit")
                    text = body.decode(response.encoding or "utf-8", errors="replace")
                    visible = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)[:10_000]
                    if BLOCKED.search(visible):
                        raise PermissionError("CAPTCHA or access restriction detected")
                    return text
        raise ToolLimitError("redirect limit reached")

    def navigate_public_page(self, url: str, source: str, timeout_ms: int = 18_000) -> str:
        if self.browser is None:
            raise RuntimeError("Playwright browser is not available")
        if not 1_000 <= timeout_ms <= 30_000:
            raise ValueError("timeout_ms must be between 1000 and 30000")
        self._consume_page()
        return self.browser.visit(url, self._domains(source), timeout=timeout_ms)

    def extract_visible_text(self, html: str) -> str:
        if len(html.encode("utf-8")) > int(self.config["max_response_bytes"]):
            raise ToolLimitError("HTML exceeds configured byte limit")
        soup = BeautifulSoup(html, "html.parser")
        for node in soup(["script", "style", "noscript", "nav", "footer"]):
            node.decompose()
        return privacy(soup.get_text(" ", strip=True), int(self.config["model_max_input_chars"]))

    def extract_listing_candidates(self, html: str, base_url: str, source: str) -> list[str]:
        self._policy(source).validate(base_url)
        return ADAPTERS[source].discover(html, base_url, self.config)

    def inspect_vehicle_detail(self, html: str, url: str, source: str) -> dict[str, Any]:
        self._policy(source).validate(url)
        if not ADAPTERS[source].is_detail(url):
            raise ValueError("URL is not a concrete vehicle-detail URL")
        return extract(html, url, source, self.config, model_budget=self.model_budget)

    def decode_vin(self, vin: str, *, remote: bool = False, year: int = 2022) -> str:
        if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin or ""):
            return "invalid"
        return validate_vin(vin, remote=remote, year=year)

    def query_existing_listings(self, source: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if source is not None and source not in ADAPTERS:
            raise ValueError("unknown source")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return self.store.query(source=source, limit=limit)

    def save_listing_evidence(self, source: str, page_number: int, html: str) -> dict[str, str]:
        if source not in ADAPTERS and source != "fixture":
            raise ValueError("unknown source")
        maximum = 100 if source == "fixture" else int(self.config["max_pages_per_source"])
        if not 1 <= page_number <= maximum:
            raise ValueError("page number exceeds configured limit")
        encoded = html.encode("utf-8")
        if len(encoded) > int(self.config["max_response_bytes"]):
            raise ToolLimitError("evidence exceeds configured byte limit")
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        html_path = self.evidence_dir / f"{source}-{page_number}.html"
        text_path = self.evidence_dir / f"{source}-{page_number}.txt"
        html_path.write_bytes(encoded)
        text_path.write_text(self.extract_visible_text(html), encoding="utf-8")
        return {"html": str(html_path), "text": str(text_path)}

    def record_source_result(self, source: str, status: str, detail: str, observed: str) -> None:
        if self.run_id is None:
            raise RuntimeError("run_id is required")
        if source not in ADAPTERS:
            raise ValueError("unknown source")
        if status not in {"ok", "empty", "blocked", "failed", "disabled", "deadline", "fixture"}:
            raise ValueError("invalid source status")
        self.store.event(self.run_id, source, status, privacy(detail, 500), observed)

    def generate_report(
        self,
        summary: dict[str, Any],
        items: list[dict[str, Any]],
        events: list[dict[str, Any]],
        changes: dict[str, list[dict[str, Any]]],
        directory: Path,
    ) -> dict[str, Any]:
        report = build(summary, items, events, changes)
        write(report, directory)
        return report
