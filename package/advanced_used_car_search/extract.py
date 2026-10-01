"""Deterministic, evidence-oriented extraction with optional bounded model suggestions."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

from .model import ModelBudget
from .security import canonical_url

PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}\b")
CA = re.compile(r"\b(?:California|CA)\b", re.IGNORECASE)
VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
ADVERSE = re.compile(
    r"\b(salvage(?:d)?(?: title)?|rebuilt(?: title)?|lemon(?: law| buyback| title)?|flood(?: damage| title)?|"
    r"junk(?: title)?|parts[- ]only|title[- ]pending|major accident|structural damage|frame damage|"
    r"auction[- ]only)\b",
    re.IGNORECASE,
)
NEGATION_NEAR = re.compile(
    r"\b(?:no|not|never|without|free of|none|isn['’]?t|wasn['’]?t|hasn['’]?t)\b"
    r"(?:\s+[A-Za-z]+){0,4}\s*$",
    re.IGNORECASE,
)
CLEAN_TITLE = re.compile(r"\b(clean title|clear title|title is clean)\b", re.IGNORECASE)
AMBIGUOUS_TITLE = re.compile(
    r"\b(title (?:status )?(?:unknown|unavailable|not shown)|ask (?:the )?dealer about title)\b",
    re.IGNORECASE,
)
PRICE = re.compile(r"\$\s*([\d,]{4,7})(?!\d)")
MILES = re.compile(r"\b([\d,]{1,7})\s*(?:mi\.?|miles)\b", re.IGNORECASE)
HARD_FIELDS = {"year", "make", "model", "price", "mileage", "location", "vin", "title_evidence", "history"}


def privacy(text: Any, limit: int = 50_000) -> str:
    return PHONE.sub("[redacted phone]", EMAIL.sub("[redacted email]", str(text or "")))[:limit]


def canonical(url: str) -> str:
    return canonical_url(url)


def adverse_evidence(text: str) -> tuple[list[str], list[str]]:
    affirmative: list[str] = []
    negated: list[str] = []
    for match in ADVERSE.finditer(text or ""):
        prefix = (text or "")[max(0, match.start() - 60) : match.start()]
        clause = re.split(r"[.!?;:\n]", prefix)[-1]
        target = negated if NEGATION_NEAR.search(clause) else affirmative
        phrase = match.group(0).strip()
        if phrase.lower() not in {item.lower() for item in target}:
            target.append(phrase)
    return affirmative, negated


def title_evidence_state(text: str) -> dict[str, Any]:
    adverse, negated = adverse_evidence(text)
    clean = CLEAN_TITLE.search(text or "")
    ambiguous = AMBIGUOUS_TITLE.search(text or "")
    if adverse:
        state = "affirmative_adverse"
    elif clean:
        state = "seller_clean_claim"
    elif negated:
        state = "negated_adverse"
    elif ambiguous:
        state = "ambiguous"
    else:
        state = "absent"
    return {
        "state": state,
        "clean_claim": clean.group(0) if clean else None,
        "adverse": adverse,
        "negated": negated,
        "ambiguous": ambiguous.group(0) if ambiguous else None,
    }


def _jsonld(soup: BeautifulSoup):
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or tag.get_text())
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                if isinstance(entry, dict):
                    yield from (item for item in entry.get("@graph", []) if isinstance(item, dict))
                    yield entry
        except (ValueError, TypeError):
            continue


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None and value != ""), None)


def _int(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("value")
    match = re.search(r"\d+", str(value or "").replace(",", ""))
    return int(match.group()) if match else None


def _brand(value: Any) -> str | None:
    if isinstance(value, dict):
        return str(value.get("name") or "") or None
    return str(value) if value else None


def extract(
    html: str,
    url: str,
    source: str,
    config: dict[str, Any],
    *,
    use_model: bool = True,
    model_budget: ModelBudget | None = None,
) -> dict[str, Any]:
    original = BeautifulSoup(html, "html.parser")
    soup = BeautifulSoup(html, "html.parser")
    for item in soup(["script", "style", "noscript", "footer", "nav"]):
        item.decompose()
    text = privacy(soup.get_text(" ", strip=True), max(50_000, int(config["model_max_input_chars"])))
    structured: dict[str, Any] = next(
        (
            obj
            for obj in _jsonld(original)
            if any(
                key in obj
                for key in ("vehicleModelDate", "mileageFromOdometer", "vehicleIdentificationNumber")
            )
        ),
        {},
    )
    offer = structured.get("offers") or {}
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    if not isinstance(offer, dict):
        offer = {}
    address = structured.get("address") or {}
    if isinstance(address, str):
        address = {"addressLocality": address}
    location = ", ".join(
        str(address[key]) for key in ("addressLocality", "addressRegion", "postalCode") if address.get(key)
    )
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    heading = soup.h1.get_text(" ", strip=True) if soup.h1 else ""
    identity = " ".join(
        [title, heading, str(structured.get("name") or ""), str(structured.get("model") or ""), text[:500]]
    )

    def field(pattern: str) -> str | None:
        match = re.search(pattern, text, re.IGNORECASE)
        return match.group(1).strip() if match else None

    title_state = title_evidence_state(text)
    history = field(
        r"\b(no accidents? reported|no accidents?|accident[- ]free|minor accident|major accident|"
        r"structural damage|damage reported|no damage reported)\b"
    )
    exterior = field(
        r"\b(?:exterior(?: color)?|ext(?:erior)?)\s*[:\-]\s*"
        r"(black|caviar|obsidian|white|silver|gray|grey|blue|red|green|brown|other)\b"
    )
    interior = field(
        r"\b(?:interior(?: color)?|int(?:erior)?)\s*[:\-]\s*"
        r"(palomino|black|beige|brown|white|gray|grey|other)\b"
    )
    match_year = re.search(r"\b(20(?:21|22|23))\b", identity)
    raw_price = _first(offer.get("price"), structured.get("price"), field(r"\$\s*([\d,]{4,7})\b"))
    raw_miles = _first(structured.get("mileageFromOdometer"), field(r"\b([\d,]{1,7})\s*(?:mi\.?|miles)\b"))
    vin = _first(
        structured.get("vehicleIdentificationNumber"), field(r"\bVIN\s*[:#]?\s*([A-HJ-NPR-Z0-9]{17})\b")
    )
    url_vin = VIN.search(unquote(url))
    vin = _first(vin, url_vin.group(0) if url_vin else None)
    deterministic: dict[str, Any] = {
        "year": _int(_first(structured.get("vehicleModelDate"), match_year.group(1) if match_year else None)),
        "make": _first(
            _brand(structured.get("brand")),
            "Lexus"
            if re.search(r"\bLexus\b", identity, re.I)
            or (source == "lexus" and re.search(r"\bES\s*300\s*h\b", identity, re.I))
            else None,
        ),
        "model": _first(
            structured.get("model"), "ES 300h" if re.search(r"\bES\s*300\s*h\b", identity, re.I) else None
        ),
        "trim": structured.get("vehicleConfiguration"),
        "price": _int(raw_price),
        "fees": _int(field(r"\b(?:dealer|documentation|doc) fees?\s*[:\-]?\s*\$([\d,]+)")),
        "location": privacy(_first(location, field(r"\b([A-Za-z .'-]{2,45},\s*CA(?:\s*\d{5})?)\b")) or ""),
        "mileage": _int(raw_miles),
        "exterior": _first(structured.get("color"), structured.get("vehicleColor"), exterior),
        "interior": _first(structured.get("vehicleInteriorColor"), interior),
        "vin": str(vin or "").upper(),
        "title_evidence": title_state["clean_claim"],
        "history": history,
        "seller_type": "dealer" if re.search(r"\b(dealership|dealer inventory)\b", text, re.I) else "unknown",
        "cpo": bool(re.search(r"\b(?:Lexus Certified|certified pre-owned|L/Certified)\b", text, re.I)),
        "posted_date": field(
            r"\b(?:posted|updated|listed)\s*(?:on)?\s*[:\-]?\s*"
            r"((?:20\d\d[-/]\d\d[-/]\d\d)|(?:\w+\s+\d{1,2},?\s+20\d\d))"
        ),
        "description": privacy(field(r"\bDescription\s*[:\-]\s*(.{30,2000})") or text[:2000]),
        "seller_name": privacy(
            structured.get("seller", {}).get("name") if isinstance(structured.get("seller"), dict) else ""
        ),
    }
    provenance: dict[str, dict[str, Any]] = {}
    structured_fields: dict[str, str] = {}
    for key, field_name in (
        ("year", "vehicleModelDate"),
        ("make", "brand"),
        ("model", "model"),
        ("trim", "vehicleConfiguration"),
        ("mileage", "mileageFromOdometer"),
        ("vin", "vehicleIdentificationNumber"),
    ):
        if structured.get(field_name) not in (None, ""):
            structured_fields[key] = field_name
    if offer.get("price") not in (None, ""):
        structured_fields["price"] = "offers.price"
    elif structured.get("price") not in (None, ""):
        structured_fields["price"] = "price"
    if structured.get("color") not in (None, ""):
        structured_fields["exterior"] = "color"
    elif structured.get("vehicleColor") not in (None, ""):
        structured_fields["exterior"] = "vehicleColor"
    if structured.get("vehicleInteriorColor") not in (None, ""):
        structured_fields["interior"] = "vehicleInteriorColor"
    if location:
        structured_fields["location"] = "address"
    for key, value in deterministic.items():
        if value in (None, "", False):
            provenance[key] = {"state": "unknown", "evidence": None}
        elif key in structured_fields:
            provenance[key] = {"state": "structured_visible", "evidence": structured_fields[key]}
        else:
            provenance[key] = {"state": "visible_text", "evidence": str(value)[:180]}
    if url_vin and deterministic["vin"] == url_vin.group(0):
        provenance["vin"] = {"state": "url_identity", "evidence": url_vin.group(0)}
    provenance["title_evidence"] = {
        "state": "seller_claim" if title_state["state"] == "seller_clean_claim" else title_state["state"],
        "evidence": title_state["clean_claim"] or title_state["ambiguous"] or title_state["negated"] or None,
    }

    suggestion = model_budget.extract(text, canonical(url)) if use_model and model_budget else None
    suggested = suggestion.model_dump() if suggestion else {}
    for key, value in suggested.items():
        if (
            key in HARD_FIELDS
            or value in (None, "", False)
            or deterministic.get(key) not in (None, "", False)
        ):
            continue
        # Every accepted model suggestion must point back to visible text.
        if key == "cpo":
            visible = bool(re.search(r"\b(?:CPO|certified pre-owned|L/Certified)\b", text, re.IGNORECASE))
        elif isinstance(value, (int, float)):
            visible = str(value) in text.replace(",", "")
        else:
            visible = bool(re.search(r"\b" + re.escape(str(value)) + r"\b", text, re.IGNORECASE))
        if not visible:
            continue
        deterministic[key] = value
        provenance[key] = {"state": "model_inferred_visible", "evidence": str(value)[:180]}

    result = {
        "url": canonical(url),
        "source": source,
        "found_at": datetime.now(UTC).isoformat(),
        **deterministic,
        "photo_count": len(original.select("img[src]")),
        "photo_hashes": [
            hashlib.sha256(canonical(urljoin(url, str(image.get("src") or ""))).encode()).hexdigest()[:20]
            for image in original.select("img[src]")[:8]
            if canonical(urljoin(url, str(image.get("src") or "")))
        ],
        "title_state": title_state["state"],
        "adverse_evidence": title_state["adverse"],
        "negated_adverse_evidence": title_state["negated"],
        "title_confidence": "seller claim (unverified)" if title_state["clean_claim"] else "unverified",
        "suspicious": [],
        "provenance": provenance,
        "model_status": "used" if suggestion else "not-used",
    }
    if result["exterior"]:
        result["exterior"] = str(result["exterior"]).lower()
    if result["interior"]:
        result["interior"] = str(result["interior"]).lower()
    if title_state["adverse"]:
        result["suspicious"].append(
            "affirmative prohibited title/history evidence: " + ", ".join(title_state["adverse"])
        )
    if not result["title_evidence"]:
        result["suspicious"].append("clean title not documented on page")
    if not result["history"]:
        result["suspicious"].append("vehicle history not documented on page")
    return result
