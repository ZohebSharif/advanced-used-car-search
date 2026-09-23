from __future__ import annotations

import pytest
import yaml
from lexus_hunter.config import load


def test_mutable_defaults_are_isolated_between_loads(tmp_path) -> None:
    values = load()
    values.pop("dealer_urls")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")

    first = load(path)
    first["dealer_urls"].append("https://dealer.example/inventory")

    assert load(path)["dealer_urls"] == []


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
