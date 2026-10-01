from __future__ import annotations

import sys
from types import SimpleNamespace

import advanced_used_car_search.sources as source_module
import pytest
from advanced_used_car_search.security import UnsafeUrlError, URLPolicy, canonical_url
from advanced_used_car_search.sources import Browser


def resolver_for(address: str):
    return lambda _host, _port: [address]


def test_url_policy_allows_only_public_allowlisted_http() -> None:
    policy = URLPolicy(("example.com",), resolver=resolver_for("93.184.216.34"))
    assert (
        policy.validate("https://www.example.com/car/1/?utm_source=x#photos")
        == "https://www.example.com/car/1/?utm_source=x"
    )
    with pytest.raises(UnsafeUrlError):
        policy.validate("https://evil.example.net/car/1")
    with pytest.raises(UnsafeUrlError):
        policy.validate("file:///etc/passwd")
    with pytest.raises(UnsafeUrlError):
        policy.validate("https://user:pass@example.com/car/1")
    with pytest.raises(UnsafeUrlError):
        policy.validate("https://example.com:8443/car/1")


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.2", "::1"])
def test_url_policy_rejects_non_global_resolution(address: str) -> None:
    with pytest.raises(UnsafeUrlError):
        URLPolicy(("example.com",), resolver=resolver_for(address)).validate("https://example.com/car/1")


class FakeRoute:
    def __init__(self, url: str, frame: object, navigation: bool = False, method: str = "GET"):
        self.request = SimpleNamespace(
            url=url,
            frame=frame,
            method=method,
            is_navigation_request=lambda: navigation,
        )

    def abort(self) -> None:
        self.action = "abort"

    def continue_(self) -> None:
        self.action = "continue"


def bare_browser() -> Browser:
    browser = object.__new__(Browser)
    browser.page = SimpleNamespace(main_frame=object())
    browser._active_policy = None
    return browser


def test_browser_route_rejects_private_subresource(monkeypatch) -> None:
    monkeypatch.setattr(
        source_module,
        "URLPolicy",
        lambda domains: URLPolicy(domains, resolver=resolver_for("127.0.0.1")),
    )
    route = FakeRoute("http://metadata.example/latest", object())
    bare_browser()._route(route)
    assert route.action == "abort"


def test_browser_route_rechecks_dns_on_every_request(monkeypatch) -> None:
    addresses = iter(("93.184.216.34", "127.0.0.1"))

    def changing_resolver(_host: str, _port: int):
        return [next(addresses)]

    monkeypatch.setattr(
        source_module,
        "URLPolicy",
        lambda domains: URLPolicy(domains, resolver=changing_resolver),
    )
    browser = bare_browser()
    first = FakeRoute("https://assets.example/image.jpg", object())
    second = FakeRoute("https://assets.example/data.json", object())
    browser._route(first)
    browser._route(second)
    assert first.action == "continue"
    assert second.action == "abort"


def test_browser_route_rejects_public_non_allowlisted_subresource(monkeypatch) -> None:
    public = resolver_for("93.184.216.34")
    monkeypatch.setattr(
        source_module,
        "URLPolicy",
        lambda domains: URLPolicy(domains, resolver=public),
    )
    browser = bare_browser()
    browser._active_policy = URLPolicy(("example.com",), resolver=public)
    route = FakeRoute("https://tracker.example.net/pixel", object())
    browser._route(route)
    assert route.action == "abort"


def test_browser_route_allows_reads_and_rejects_mutating_methods(monkeypatch) -> None:
    public = resolver_for("93.184.216.34")
    monkeypatch.setattr(
        source_module,
        "URLPolicy",
        lambda domains: URLPolicy(domains, resolver=public),
    )
    browser = bare_browser()
    browser._active_policy = URLPolicy(("example.com",), resolver=public)
    get_route = FakeRoute("https://example.com/vehicle/1", object(), method="GET")
    post_route = FakeRoute("https://example.com/contact", object(), method="POST")
    browser._route(get_route)
    browser._route(post_route)
    assert get_route.action == "continue"
    assert post_route.action == "abort"


def test_browser_blocks_service_workers_and_websockets(monkeypatch) -> None:
    context_options = {}
    context_routes = []
    websocket_routes = []

    class FakePage:
        pass

    class FakeContext:
        def new_page(self):
            return FakePage()

        def route(self, pattern, handler):
            context_routes.append((pattern, handler))

        def route_web_socket(self, pattern, handler):
            websocket_routes.append((pattern, handler))

    class FakeChromium:
        def launch(self, *, headless):
            return SimpleNamespace(
                new_context=lambda **kwargs: context_options.update(kwargs) or FakeContext()
            )

    manager = SimpleNamespace(chromium=FakeChromium())
    starter = SimpleNamespace(start=lambda: manager)
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: starter),
    )

    Browser(headless=True)

    assert context_options["service_workers"] == "block"
    assert context_routes and context_routes[0][0] == "**/*"
    assert websocket_routes and websocket_routes[0][0] == "**/*"
    closed = []
    websocket_routes[0][1](SimpleNamespace(close=lambda: closed.append(True)))
    assert closed == [True]


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
def test_browser_rejects_access_control_signals(signal: str) -> None:
    browser = object.__new__(Browser)
    browser.max_response_bytes = 10_000
    with pytest.raises(PermissionError):
        browser._checked_html(f"<html><body>{signal}</body></html>")


def test_browser_allows_ordinary_subscribe_footer() -> None:
    browser = object.__new__(Browser)
    browser.max_response_bytes = 10_000
    html = "<html><body>Vehicle inventory<footer>Subscribe for weekly updates</footer></body></html>"

    assert browser._checked_html(html) == html


def test_canonical_url_removes_tracking_but_preserves_identity_query() -> None:
    url = "https://EXAMPLE.com/car/1?listingId=42&utm_campaign=x#photos"
    assert canonical_url(url) == "https://example.com/car/1?listingId=42"
