from __future__ import annotations

from pathlib import Path

from lexus_hunter.config import load
from lexus_hunter.security import is_vehicle_detail_url
from lexus_hunter.sources import ADAPTERS

SAVED = Path(__file__).parent / "fixtures" / "saved_cargurus_search_2026-09-23.html"
BASE = "https://www.cargurus.com/Cars/l-Used-2022-Lexus-ES-Hybrid-c31882?zip=95112"


def test_saved_public_search_page_yields_only_concrete_detail_urls() -> None:
    html = SAVED.read_text(encoding="utf-8")
    links = ADAPTERS["cargurus"].discover(html, BASE, load())
    assert not is_vehicle_detail_url(BASE)
    assert links == [
        "https://www.cargurus.com/details/450382765?listingIndex=1&searchDistance=50",
        "https://www.cargurus.com/details/452988312?listingIndex=5&searchDistance=50",
    ]
    assert all(is_vehicle_detail_url(url) for url in links)
