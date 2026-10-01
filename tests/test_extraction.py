from __future__ import annotations

from pathlib import Path

from advanced_used_car_search.config import load
from advanced_used_car_search.extract import extract, title_evidence_state
from advanced_used_car_search.model import ExtractionSuggestion, ModelBudget, ModelCall
from advanced_used_car_search.rank import evaluate

CONFIG = load()


def test_fixture_location_does_not_include_previous_sentence() -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "autotrader_near.html"
    item = extract(
        fixture.read_text(), "https://example.com/vehicle/near", "fixture", CONFIG, use_model=False
    )
    assert item["location"] == "Sacramento, CA 95814"


def test_title_language_is_context_aware() -> None:
    assert title_evidence_state("This vehicle has a salvage title.")["state"] == "affirmative_adverse"
    assert title_evidence_state("No salvage title. Not rebuilt.")["state"] == "negated_adverse"
    assert (
        title_evidence_state("Title status unavailable; ask the dealer about title.")["state"] == "ambiguous"
    )
    assert title_evidence_state("One owner with service records.")["state"] == "absent"
    mixed = title_evidence_state("Not rebuilt. Major accident reported.")
    assert mixed["state"] == "affirmative_adverse"
    assert mixed["adverse"] == ["Major accident"]
    assert mixed["negated"] == ["rebuilt"]


def test_negated_adverse_phrase_does_not_trigger_avoid() -> None:
    html = """
    <title>2022 Lexus ES 300h</title><h1>2022 Lexus ES 300h</h1>
    <p>$29,000 30,000 miles San Jose, CA 95112.</p>
    <p>Clean title. No salvage title. Not rebuilt. No major accident.</p>
    <p>Exterior: black Interior: palomino.</p>
    """
    item = extract(html, "https://example.com/vehicle/123", "fixture", CONFIG, use_model=False)
    assert item["title_state"] == "seller_clean_claim"
    assert item["adverse_evidence"] == []
    assert set(item["negated_adverse_evidence"]) == {"salvage title", "rebuilt", "major accident"}
    assert evaluate(item, CONFIG, vin_status="missing")["category"] != "avoid"
    assert item["provenance"]["title_evidence"]["state"] == "seller_claim"


def test_clean_history_is_not_clean_title() -> None:
    html = """
    <title>2022 Lexus ES 300h</title><h1>2022 Lexus ES 300h</h1>
    <p>$29,000 30,000 miles San Jose, CA 95112. Clean Carfax, no accidents reported.</p>
    <p>Exterior: black Interior: palomino.</p>
    """
    item = extract(html, "https://example.com/vehicle/124", "fixture", CONFIG, use_model=False)
    assert item["title_state"] == "absent"
    assert item["title_evidence"] is None
    assert evaluate(item, CONFIG, vin_status="missing")["category"] == "avoid"


def test_visible_price_is_not_mislabeled_as_structured_evidence() -> None:
    html = """
    <script type="application/ld+json">
      {
        "@type": "Vehicle",
        "vehicleModelDate": "2022",
        "brand": {"name": "Lexus"},
        "model": "ES 300h",
        "offers": {"availability": "https://schema.org/InStock"}
      }
    </script>
    <h1>2022 Lexus ES 300h</h1>
    <p>$29,000 · 30,000 miles · San Jose, CA 95112</p>
    """

    item = extract(html, "https://example.com/vehicle/structured", "fixture", CONFIG, use_model=False)

    assert item["price"] == 29_000
    assert item["provenance"]["price"]["state"] == "visible_text"


def test_lexus_overlay_text_provides_vehicle_identity() -> None:
    html = """
    <html><body>
      DETAILS 2022 ES 300h LUXURY $40,995 30,837 MILES
      Location 34.2 MILES AWAY Located at: Tustin Lexus, Tustin, CA 92782
    </body></html>
    """
    item = extract(
        html,
        "https://www.lexus.com/lcertified/search-inventory"
        "?link[LcertSearchInventory][setVin]=58AEA1C16NU018844",
        "lexus",
        CONFIG,
        use_model=False,
    )
    assert (item["year"], item["make"], item["model"]) == (2022, "Lexus", "ES 300h")
    assert (item["price"], item["mileage"], item["vin"]) == (40_995, 30_837, "58AEA1C16NU018844")
    assert item["provenance"]["vin"] == {
        "state": "url_identity",
        "evidence": "58AEA1C16NU018844",
    }


class SuggestingClient:
    available = True
    model_name = "fake"

    def extract(self, visible_text: str, url: str):
        suggestion = ExtractionSuggestion(year=2023, price=1, vin="AAAAAAAAAAAAAAAAA", cpo=True)
        return suggestion, ModelCall("used", "fake", 1, len(visible_text), 1)


def test_model_suggestion_cannot_override_hard_evidence() -> None:
    html = """
    <title>2022 Lexus ES 300h</title><h1>2022 Lexus ES 300h</h1>
    <p>$29,000 30,000 miles San Jose, CA. Clean title. Exterior: black Interior: palomino.</p>
    """
    budget = ModelBudget(SuggestingClient(), 1)
    item = extract(
        html,
        "https://example.com/vehicle/125",
        "fixture",
        CONFIG,
        use_model=True,
        model_budget=budget,
    )
    assert (item["year"], item["price"], item["vin"]) == (2022, 29_000, "")
    assert item["cpo"] is False
    assert item["provenance"]["cpo"]["state"] == "unknown"
    assert item["model_status"] == "used"
    assert budget.summary()["calls"] == 1
