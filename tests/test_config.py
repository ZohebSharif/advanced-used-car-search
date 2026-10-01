from __future__ import annotations

import pytest
import yaml
from advanced_used_car_search.config import load


def test_mutable_defaults_are_isolated_between_loads(tmp_path) -> None:
    values = load()
    values.pop("dealer_urls")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")

    first = load(path)
    first["dealer_urls"].append("https://dealer.example/inventory")

    assert load(path)["dealer_urls"] == []


def test_explicit_source_order_is_preserved() -> None:
    order = ["autotrader", "lexus", "dealers"]
    assert load(enabled_sources=order)["enabled_sources"] == order


def test_duplicate_sources_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        load(enabled_sources=["lexus", "lexus"])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_pages_per_source", 0),
        ("max_response_bytes", 0),
        ("request_delay_seconds", -0.1),
        ("stale_after_days", -1),
        ("output_retention_days", -1),
        ("minimum_deal_score", 101),
    ],
)
def test_bounded_runtime_settings_are_validated(key: str, value: object) -> None:
    with pytest.raises(ValueError):
        load(**{key: value})


@pytest.mark.parametrize(
    "url",
    [
        "http://api.deepseek.com",
        "https://user:password@api.deepseek.com",
        "https://api.deepseek.com:8443",
        "https://127.0.0.1",
    ],
)
def test_model_base_url_rejects_unsafe_shapes(url: str) -> None:
    with pytest.raises(ValueError):
        load(model_base_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:password@dealer.example/inventory",
        "https://dealer.example:8443/inventory",
    ],
)
def test_dealer_urls_reject_unsafe_shapes(url: str) -> None:
    with pytest.raises(ValueError):
        load(dealer_urls=[url])


@pytest.mark.parametrize("radius", [0, -1, 10.5, True])
def test_search_radius_requires_positive_integer(radius) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        load(search_radius_miles=radius)


@pytest.mark.parametrize("radius", [250, 501])
def test_search_radius_allows_non_lexus_distances(radius: int) -> None:
    assert load(search_radius_miles=radius)["search_radius_miles"] == radius
