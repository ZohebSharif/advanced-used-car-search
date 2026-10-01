from __future__ import annotations

from types import SimpleNamespace

import pytest
from advanced_used_car_search.config import load
from advanced_used_car_search.security import UnsafeUrlError, URLPolicy
from advanced_used_car_search.store import Store
from advanced_used_car_search.tools import AgentTools


class Response:
    def __init__(
        self,
        status_code: int,
        *,
        body: bytes = b"",
        location: str | None = None,
        peer_address: str = "93.184.216.34",
    ) -> None:
        self.status_code = status_code
        self.headers = {"location": location} if location else {}
        self.extensions = {
            "network_stream": SimpleNamespace(
                get_extra_info=lambda name: (peer_address, 443)
            )
        }
        self.url = "https://www.autotrader.com/search"
        self.encoding = "utf-8"
        self._body = body
        self.body_reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self):
        self.body_reads += 1
        yield self._body


class Client:
    def __init__(self, response: Response, calls: list[tuple[str, str]], **kwargs) -> None:
        self.response = response
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def stream(self, method: str, url: str) -> Response:
        self.calls.append((method, url))
        return self.response


def tools(tmp_path) -> AgentTools:
    config = load(request_delay_seconds=0)
    return AgentTools(config, Store(tmp_path / "test.sqlite3"), tmp_path / "evidence")


@pytest.fixture(autouse=True)
def offline_url_policy(monkeypatch) -> None:
    monkeypatch.setattr(
        AgentTools,
        "_policy",
        lambda self, source: URLPolicy(
            self._domains(source),
            resolver=lambda host, port: ["93.184.216.34"],
        ),
    )


@pytest.mark.parametrize("status_code", [401, 403, 429])
def test_http_fetch_blocks_access_restrictions_without_retry(
    tmp_path, monkeypatch, status_code: int
) -> None:
    calls = []
    response = Response(status_code)
    monkeypatch.setattr(
        "advanced_used_car_search.tools.httpx.Client",
        lambda **kwargs: Client(response, calls, **kwargs),
    )

    with pytest.raises(PermissionError):
        tools(tmp_path).fetch_public_page(
            "https://www.autotrader.com/cars-for-sale/vehicle/1", "autotrader"
        )

    assert calls == [
        ("GET", "https://www.autotrader.com/cars-for-sale/vehicle/1")
    ]


def test_http_fetch_revalidates_redirect_destination(tmp_path, monkeypatch) -> None:
    calls = []
    response = Response(302, location="https://evil.example/vehicle/1")
    monkeypatch.setattr(
        "advanced_used_car_search.tools.httpx.Client",
        lambda **kwargs: Client(response, calls, **kwargs),
    )

    with pytest.raises(UnsafeUrlError):
        tools(tmp_path).fetch_public_page(
            "https://www.autotrader.com/cars-for-sale/vehicle/1", "autotrader"
        )

    assert len(calls) == 1


def test_http_fetch_rejects_private_peer_before_reading_body(tmp_path, monkeypatch) -> None:
    class UnreadableBodyResponse(Response):
        def iter_bytes(self):
            raise AssertionError("private-peer response body must not be consumed")

    calls = []
    response = UnreadableBodyResponse(
        200,
        body=b"must not be consumed",
        peer_address="127.0.0.1",
    )
    monkeypatch.setattr(
        "advanced_used_car_search.tools.httpx.Client",
        lambda **kwargs: Client(response, calls, **kwargs),
    )

    with pytest.raises(UnsafeUrlError):
        tools(tmp_path).fetch_public_page(
            "https://www.autotrader.com/cars-for-sale/vehicle/1", "autotrader"
        )

    assert len(calls) == 1
    assert response.body_reads == 0


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
def test_http_fetch_blocks_access_control_pages(tmp_path, monkeypatch, signal: str) -> None:
    calls = []
    response = Response(200, body=f"<html>{signal}</html>".encode())
    monkeypatch.setattr(
        "advanced_used_car_search.tools.httpx.Client",
        lambda **kwargs: Client(response, calls, **kwargs),
    )

    with pytest.raises(PermissionError):
        tools(tmp_path).fetch_public_page(
            "https://www.autotrader.com/cars-for-sale/vehicle/1", "autotrader"
        )

    assert len(calls) == 1
