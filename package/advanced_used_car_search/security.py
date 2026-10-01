from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "msclkid",
    "ref",
    "referrer",
    "source",
}
DETAIL_PATH = re.compile(
    r"/marketplace/item/\d+|/vehicledetails?(?:/|\.)|/vehicle/[^/]+|/shopping/[^/]+/\d+|"
    r"/cars/[^/]+/\d+|/inventory/(?:[^/]+/){1,}[^/]+|/listing/(?:[A-HJ-NPR-Z0-9]{17}|\d{5,})|"
    r"/car/[^/]+/\d+|/details/\d+|/(?:cto|ctd)/\d+",
    re.IGNORECASE,
)


class UnsafeUrlError(ValueError):
    pass


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
        )
    )
    host = parsed.hostname.lower().removeprefix("www.")
    port = f":{parsed.port}" if parsed.port and parsed.port not in {80, 443} else ""
    return urlunparse((parsed.scheme.lower(), host + port, parsed.path.rstrip("/"), "", query, ""))


def is_vehicle_detail_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    return DETAIL_PATH.search(parsed.path) is not None


def _default_resolver(host: str, port: int) -> Iterable[str]:
    return {str(entry[4][0]) for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}


@dataclass(frozen=True)
class URLPolicy:
    allowed_domains: tuple[str, ...]
    resolver: Callable[[str, int], Iterable[str]] = _default_resolver

    def validate(self, url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"}:
            raise UnsafeUrlError("only HTTP and HTTPS URLs are allowed")
        if not host or parsed.username or parsed.password:
            raise UnsafeUrlError("URL host or credentials are invalid")
        if not self._allowed(host):
            raise UnsafeUrlError(f"host is not allowlisted: {host}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in {80, 443}:
            raise UnsafeUrlError(f"unsafe port rejected: {port}")
        try:
            addresses = tuple(self.resolver(host, port))
        except OSError as exc:
            raise UnsafeUrlError(f"host resolution failed: {host}") from exc
        if not addresses:
            raise UnsafeUrlError(f"host did not resolve: {host}")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address.split("%")[0])
            except ValueError as exc:
                raise UnsafeUrlError(f"resolver returned an invalid address for {host}") from exc
            if not ip.is_global:
                raise UnsafeUrlError(f"host resolves to a non-public address: {host}")
        return urlunparse(parsed._replace(fragment=""))

    def _allowed(self, host: str) -> bool:
        return any(host == domain or host.endswith("." + domain) for domain in self.allowed_domains)


def domains_from_urls(urls: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            host for url in urls if (host := (urlparse(url).hostname or "").lower().removeprefix("www."))
        )
    )
