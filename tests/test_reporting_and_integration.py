from __future__ import annotations

import os

import pytest

from lexus_hunter.config import load
from lexus_hunter.model import DeepSeekClient
from lexus_hunter.report import build, write


def candidate():
    return {
        "score": 88,
        "category": "excellent deal",
        "year": 2022,
        "price": 29_000,
        "mileage": 30_000,
        "location": "San Jose, CA",
        "url": "https://example.com/vehicle/1",
        "source": "autotrader",
        "exterior": "black",
        "interior": "palomino",
        "cpo": False,
        "title_state": "seller_clean_claim",
        "title_confidence": "seller claim (unverified)",
        "vin_status": "missing",
        "first_seen": "2026-09-23T00:00:00+00:00",
        "last_seen": "2026-09-23T00:00:00+00:00",
        "missing_runs": 0,
        "relisted": True,
        "stale": False,
        "flags": ["VIN needs manual verification: missing"],
        "provenance": {"price": {"state": "structured_visible"}, "title_evidence": {"state": "seller_claim"}},
    }


def test_report_exposes_provenance_lifecycle_model_and_source_failures(tmp_path) -> None:
    item = candidate()
    summary = {
        "started": "2026-09-23T00:00:00+00:00",
        "finished": "2026-09-23T00:00:01+00:00",
        "duration_seconds": 1.0,
        "mode": "live browser",
        "sources_searched": ["autotrader"],
        "discovered": 1,
        "deduplicated": 0,
        "excluded": 0,
        "ranked": 1,
        "model_usage": {
            "model": "deepseek-flash",
            "enabled": True,
            "calls": 1,
            "max_calls": 3,
            "total_input_chars": 200,
        },
    }
    events = [{"source": "truecar", "status": "blocked", "detail": "HTTP 403"}]
    report = build(summary, [item], events, {"new": [item], "drops": [], "relisted": [item], "stale": []})
    write(report, tmp_path)
    markdown = (tmp_path / "latest.md").read_text(encoding="utf-8")
    assert "Research shortlist (top 3; manual verification required)" in markdown
    assert "seller/source claim, not independently verified" in markdown
    assert "Evidence provenance" in markdown and "title_evidence=seller_claim" in markdown
    assert "relisted: True" in markdown
    assert "truecar: blocked — HTTP 403" in markdown
    assert "calls=1/3" in markdown


@pytest.mark.skipif(
    os.getenv("RUN_DEEPSEEK_INTEGRATION") != "1" or not os.getenv("DEEPSEEK_API_KEY"),
    reason="set RUN_DEEPSEEK_INTEGRATION=1 and DEEPSEEK_API_KEY for one real bounded call",
)
def test_real_deepseek_json_mode_opt_in() -> None:
    config = load(model_enabled=True, model_max_input_chars=500, model_timeout_seconds=20)
    suggestion, event = DeepSeekClient(config).extract(
        "Visible listing text: 2022 Lexus ES 300h Luxury. Exterior: black. Interior: palomino.",
        "https://example.com/vehicle/integration",
    )
    assert event.status == "used"
    assert suggestion is not None and suggestion.trim == "Luxury"
