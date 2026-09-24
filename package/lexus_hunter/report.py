from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RANKED = {"excellent deal", "strong candidate", "worth watching"}
NEAR = {"near match", "fallback year"}
NO_TRUSTWORTHY = "No trustworthy listings found."


def build(
    summary: dict[str, Any],
    items: list[dict[str, Any]],
    events: list[dict[str, Any]],
    changes: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    exact = sorted((item for item in items if item["category"] in RANKED), key=lambda item: -item["score"])
    near = sorted((item for item in items if item["category"] in NEAR), key=lambda item: -item["score"])
    verify = [
        item
        for item in items
        if item.get("flags")
        or item.get("title_confidence") != "verified"
        or item.get("vin_status") in {"missing", "unchecked", "unavailable"}
    ]
    summary.update(
        exact_matches=exact,
        near_matches=near,
        manual_verification=verify,
        new_listings=changes.get("new", []),
        price_drops=changes.get("drops", []),
        relisted_listings=changes.get("relisted", []),
        stale_listings=changes.get("stale", []),
        source_events=events,
        shortlist=exact[:3],
        trustworthy_listings_found=bool(exact),
        trustworthy_listing_message=None if exact else NO_TRUSTWORTHY,
    )
    return summary


def reasons(item: dict[str, Any]) -> str:
    values = []
    if item.get("price") is not None:
        values.append(f"${item['price']:,} asking")
    if item.get("mileage") is not None:
        values.append(f"{item['mileage']:,} miles")
    if item.get("exterior"):
        values.append(str(item["exterior"]) + " exterior")
    if item.get("interior"):
        values.append(str(item["interior"]) + " interior")
    if item.get("cpo"):
        values.append("CPO claim visible")
    values.append("title is a seller/source claim, not independently verified")
    return ", ".join(values)


def _provenance_summary(item: dict[str, Any]) -> str:
    provenance = item.get("provenance") or {}
    interesting = ("price", "mileage", "location", "vin", "title_evidence", "exterior", "interior")
    return ", ".join(f"{field}={provenance.get(field, {}).get('state', 'unknown')}" for field in interesting)


def write(report: dict[str, Any], directory: str | Path) -> str:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "latest.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    usage = report.get("model_usage") or {}
    lines = [
        "# Lexus Hunter — latest run",
        "",
        f"Run: {report['started']} to {report['finished']} ({report['duration_seconds']:.1f}s)",
        f"Mode: {report['mode']}",
        (
            f"Model: {usage.get('model', 'none')}; enabled={usage.get('enabled', False)}; "
            f"calls={usage.get('calls', 0)}/{usage.get('max_calls', 0)}; "
            f"input chars={usage.get('total_input_chars', 0)}"
        ),
        f"Sources successfully searched: {', '.join(report['sources_searched']) or 'none'}",
        (
            f"Discovered: {report['discovered']} | deduplicated: {report['deduplicated']} | "
            f"excluded: {report['excluded']} | ranked: {report['ranked']} | "
            f"relisted: {len(report['relisted_listings'])} | stale: {len(report['stale_listings'])}"
        ),
    ]
    if report["trustworthy_listing_message"]:
        lines.extend(["", f"**{report['trustworthy_listing_message']}**"])

    def group(title: str, group_items: list[dict[str, Any]]) -> None:
        lines.extend(["", "## " + title])
        if not group_items:
            lines.append("None.")
        for item in group_items:
            lines.append(
                f"- **{item['score']}/100 {item['category']}** — {item.get('year') or '?'} Lexus ES 300h | "
                f"${item.get('price') or '?'} | {item.get('mileage') or '?'} mi | "
                f"{item.get('location') or 'location unknown'} | [Vehicle detail]({item['url']})"
            )
            if title.startswith("Research shortlist"):
                lines.append("  - Why: " + reasons(item))
            lines.append(
                "  - "
                + "; ".join(
                    (
                        f"title state: {item.get('title_state', 'absent')}",
                        f"title confidence: {item.get('title_confidence', 'unverified')}",
                        f"VIN: {item.get('vin_status')}",
                        f"first seen: {item.get('first_seen')}",
                        f"last seen: {item.get('last_seen')}",
                        f"missing runs: {item.get('missing_runs', 0)}",
                        f"relisted: {item.get('relisted', False)}",
                        f"stale: {item.get('stale', False)}",
                        f"flags: {', '.join(item.get('flags') or []) or 'none'}",
                    )
                )
            )
            lines.append("  - Evidence provenance: " + _provenance_summary(item))

    group("Research shortlist (top 3; manual verification required)", report["shortlist"])
    group("Top exact matches", report["exact_matches"])
    group("Near matches / optional fallback years", report["near_matches"])
    group("New listings since prior run", report["new_listings"])
    group("Price drops since prior run", report["price_drops"])
    group("Relisted inventory", report["relisted_listings"])
    group("Stale inventory", report["stale_listings"])
    group("Needs manual verification", report["manual_verification"])
    lines.extend(["", "## Source access / failures"])
    lines.extend(
        f"- {event['source']}: {event['status']} — {event['detail']}" for event in report["source_events"]
    )
    lines.extend(
        [
            "",
            "Research only. No seller contact or transaction actions exist. "
            "Seller/source title and history claims are not independent verification. "
            "Model-inferred values are explicitly marked and never override hard vehicle, "
            "price, mileage, state, VIN, title, or URL validation. Third-party sites may "
            "block automation; an empty result is not proof that no listing exists.",
        ]
    )
    text = "\n".join(lines) + "\n"
    (directory / "latest.md").write_text(text, encoding="utf-8")
    return "\n".join(lines[:7])


def table(items: list[dict[str, Any]]) -> None:
    if not any(item.get("category") in RANKED for item in items):
        print(NO_TRUSTWORTHY)
    print(f"{'SCORE':>5}  {'CATEGORY':<18} {'PRICE':>9} {'MILES':>8} {'LIFE':<9} SOURCE  URL")
    for item in sorted(items, key=lambda candidate: -candidate.get("score", 0))[:20]:
        lifecycle = "relisted" if item.get("relisted") else "stale" if item.get("stale") else "active"
        print(
            f"{item.get('score', 0):>5}  {item.get('category', 'unknown'):<18} "
            f"{str(item.get('price') or '?'):>9} {str(item.get('mileage') or '?'):>8} "
            f"{lifecycle:<9} {item.get('source', '?')}  {item.get('url', '')}"
        )
