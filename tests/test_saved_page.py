from __future__ import annotations

from pathlib import Path

from advanced_used_car_search.config import load
from advanced_used_car_search.security import is_vehicle_detail_url
from advanced_used_car_search.sources import ADAPTERS

SAVED = Path(__file__).parent / "fixtures" / "saved_cargurus_search_2026-09-23.html"
BASE = "https://www.cargurus.com/Cars/l-Used-2022-Lexus-ES-Hybrid-c31882?zip=95112"
LEXUS_SAVED = Path(__file__).parent / "fixtures" / "saved_lexus_inventory_2026-09-23.html"
LEXUS_BASE = "https://www.lexus.com/lcertified/search-inventory?zip=95112"


def test_saved_public_search_page_yields_only_concrete_detail_urls() -> None:
    html = SAVED.read_text(encoding="utf-8")
    links = ADAPTERS["cargurus"].discover(html, BASE, load())
    assert not is_vehicle_detail_url(BASE)
    assert links == [
        "https://www.cargurus.com/details/450382765?listingIndex=1&searchDistance=50",
        "https://www.cargurus.com/details/452988312?listingIndex=5&searchDistance=50",
    ]
    assert all(is_vehicle_detail_url(url) for url in links)


def test_saved_lexus_inventory_yields_vin_detail_overlay_link() -> None:
    links = ADAPTERS["lexus"].discover(
        LEXUS_SAVED.read_text(encoding="utf-8"),
        LEXUS_BASE,
        load(),
    )
    assert links == [
        "https://www.lexus.com/lcertified/search-inventory"
        "?link[LcertSearchInventory][setVin]=58AEA1C16NU018844"
    ]
    assert ADAPTERS["lexus"].is_detail(links[0])
