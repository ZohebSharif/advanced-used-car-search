from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from .extract import CA

VIN_CHARS = "0123456789X"


def vin_check_digit(vin: str) -> bool:
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin or ""):
        return False
    values = dict(
        zip(
            "ABCDEFGHJKLMNPRSTUVWXYZ",
            [1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 7, 9, 2, 3, 4, 5, 6, 7, 8, 9],
            strict=True,
        )
    )
    values.update({str(value): value for value in range(10)})
    weights = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
    return (
        VIN_CHARS[sum(values[char] * weight for char, weight in zip(vin, weights, strict=True)) % 11]
        == vin[8]
    )


def validate_vin(vin: str, remote: bool = True, year: int = 2022) -> str:
    if not vin:
        return "missing"
    if not vin_check_digit(vin):
        return "invalid"
    if not remote:
        return "unchecked"
    try:
        url = (
            "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/"
            + quote(vin)
            + f"?format=json&modelyear={year}"
        )
        with httpx.Client(timeout=7, follow_redirects=False, trust_env=False) as client:
            response = client.get(url)
            if response.status_code in {301, 302, 303, 307, 308}:
                return "unavailable"
            if response.status_code in {401, 403, 429}:
                return "blocked"
            response.raise_for_status()
            item = response.json()["Results"][0]
        if str(item.get("ErrorCode", "")).strip() != "0":
            return "invalid"
        make = str(item.get("Make", "")).upper()
        model = str(item.get("Model", "")).upper().replace(" ", "")
        if str(item.get("ModelYear")) != str(year) or "LEXUS" not in make or "ES" not in model:
            return "mismatch"
        hybrid_text = " ".join(
            str(item.get(key) or "")
            for key in (
                "ElectrificationLevel",
                "FuelTypePrimary",
                "FuelTypeSecondary",
                "Series",
                "Trim",
                "Model",
            )
        ).upper()
        return "verified" if "HYBRID" in hybrid_text or "HEV" in hybrid_text else "model-unconfirmed"
    except Exception:
        return "unavailable"


def evaluate(
    item: dict[str, Any],
    config: dict[str, Any],
    comparables: tuple[int, ...] | list[int] = (),
    vin_status: str | None = None,
) -> dict[str, Any]:
    result = dict(item)
    result["vin_status"] = vin_status or validate_vin(result.get("vin", ""))
    flags = list(result.get("suspicious") or [])
    identity = f"{result.get('make') or ''} {result.get('model') or ''}".lower()
    if not re.search(r"\blexus\b", identity) or not re.search(r"\bes\s*300\s*h\b", identity):
        flags.append("vehicle identity not confirmed as Lexus ES 300h")
    year = result.get("year")
    if year != config["target_year"] and not (config.get("allow_fallback_years") and year in (2021, 2023)):
        flags.append("wrong or unconfirmed model year")
    if not CA.search(result.get("location") or ""):
        flags.append("California location not confirmed")
    if result["vin_status"] in {"invalid", "mismatch", "model-unconfirmed"}:
        flags.append("VIN failed deterministic validation: " + result["vin_status"])
    elif result["vin_status"] in {"missing", "unavailable", "unchecked", "blocked"}:
        flags.append("VIN needs manual verification: " + result["vin_status"])
    price, miles = result.get("price"), result.get("mileage")
    if price is None or miles is None:
        flags.append("price or mileage missing")
    if price is not None and price > config["stretch_price"]:
        flags.append("price above stretch limit")
    if (
        price
        and config["target_price"] < price <= config["stretch_price"]
        and not (
            miles is not None and miles < 40_000 and result.get("history") and result.get("title_evidence")
        )
    ):
        flags.append("stretch requires under 40k miles plus visible title and history claims")
    if result.get("adverse_evidence"):
        flags.append("affirmative prohibited title/history signal")
    if not result.get("title_evidence"):
        flags.append("clean-title claim missing")
    if not result.get("history"):
        flags.append("history unknown")

    hard_flags = {
        "vehicle identity not confirmed as Lexus ES 300h",
        "wrong or unconfirmed model year",
        "California location not confirmed",
        "price or mileage missing",
        "price above stretch limit",
        "stretch requires under 40k miles plus visible title and history claims",
        "affirmative prohibited title/history signal",
        "clean-title claim missing",
    }
    hard = any(flag in hard_flags for flag in flags) or result["vin_status"] in {
        "invalid",
        "mismatch",
        "model-unconfirmed",
    }
    if miles is not None and miles >= config["max_mileage"]:
        flags.append("mileage above preferred maximum")
    comparable_prices = sorted(
        value for value in comparables if isinstance(value, (int, float)) and value > 0
    )
    median = comparable_prices[len(comparable_prices) // 2] if len(comparable_prices) >= 3 else None
    price_points = 14 if not median or price is None else max(0, min(25, 13 + (median - price) / median * 90))
    if price is not None and price <= config["target_price"]:
        price_points += 8
    score = (
        12
        + price_points
        + (18 if miles is not None and miles < 40_000 else 11 if miles is not None and miles < 60_000 else 3)
    )
    score += 8 if result.get("title_evidence") else 0
    score += (
        6
        if result.get("history") and re.search(r"no accidents?|accident[- ]free", result["history"], re.I)
        else 0
    )
    exterior = str(result.get("exterior") or "").lower()
    interior = str(result.get("interior") or "").lower()
    score += 6 if any(color in exterior for color in config["preferred_exterior_colors"]) else 0
    score += 5 if any(color in interior for color in config["preferred_interior_colors"]) else 0
    score += 5 if result.get("location") and CA.search(result["location"]) else 0
    score += 4 if result.get("cpo") else 0
    score += 3 if result.get("fees") is not None else 0
    score += 3 if result.get("photo_count") else 0
    try:
        age = (
            datetime.now(UTC).date() - datetime.fromisoformat(str(result.get("posted_date"))[:10]).date()
        ).days
        score += 4 if 0 <= age < 14 else 0
    except (ValueError, TypeError):
        pass
    score -= 5 if not result.get("history") else 0
    score -= 6 if not exterior or not interior else 0
    score -= min(20, 5 * len(result.get("adverse_evidence") or []))
    score = round(max(0, min(100, score)))
    result["score"] = score
    result["market_comparable_median"] = median
    result["flags"] = list(dict.fromkeys(flags))
    preferred_colors = any(color in exterior for color in config["preferred_exterior_colors"]) and any(
        color in interior for color in config["preferred_interior_colors"]
    )
    if hard:
        category = "avoid"
    elif year != 2022:
        category = "fallback year"
    elif miles >= config["max_mileage"] or price > config["target_price"] or not preferred_colors:
        category = "near match"
    elif score >= 85:
        category = "excellent deal"
    elif score >= 75:
        category = "strong candidate"
    elif score >= 65:
        category = "worth watching"
    else:
        category = "avoid"
    if category not in {"avoid", "near match", "fallback year"} and score < config["minimum_deal_score"]:
        category = "avoid"
    result["category"] = category
    return result
