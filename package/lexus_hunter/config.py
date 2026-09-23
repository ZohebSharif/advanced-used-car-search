from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_source_root = Path(__file__).resolve().parent.parent
_cwd = Path.cwd().resolve()
ROOT = (
    Path(os.environ["LEXUS_HUNTER_HOME"]).expanduser().resolve()
    if os.getenv("LEXUS_HUNTER_HOME")
    else next((p for p in (_source_root, _cwd, _cwd / "lexus-hunter") if (p / "config.yaml").exists()), _cwd)
)
load_dotenv(ROOT / ".env", override=False)

SOURCES = (
    "facebook",
    "autotrader",
    "cars",
    "cargurus",
    "truecar",
    "edmunds",
    "lexus",
    "dealers",
    "craigslist",
    "search",
)

_MODEL_ENV: dict[str, tuple[str, Any]] = {
    "DEEPSEEK_BASE_URL": ("model_base_url", str),
    "DEEPSEEK_MODEL": ("model_name", str),
    "MODEL_ENABLED": ("model_enabled", "bool"),
    "MODEL_MAX_CALLS_PER_RUN": ("model_max_calls_per_run", int),
    "MODEL_MAX_INPUT_CHARS": ("model_max_input_chars", int),
    "MODEL_TIMEOUT_SECONDS": ("model_timeout_seconds", float),
}


def _bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean environment value: {value!r}")


def _environment_overrides() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for env_name, (key, converter) in _MODEL_ENV.items():
        value = os.getenv(env_name)
        if value is None:
            continue
        result[key] = _bool(value) if converter == "bool" else converter(value)
    return result


def load(path: str | Path | None = None, **overrides: Any) -> dict[str, Any]:
    config_path = Path(path) if path else ROOT / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a mapping")
    config.update(_environment_overrides())
    config.update({key: value for key, value in overrides.items() if value is not None})
    config.setdefault("model_enabled", False)
    config.setdefault("model_base_url", "https://api.deepseek.com")
    config.setdefault("model_name", "deepseek-flash")
    config.setdefault("model_max_calls_per_run", 3)
    config.setdefault("model_max_input_chars", 12_000)
    config.setdefault("model_timeout_seconds", 20.0)
    config.setdefault("model_max_output_tokens", 800)
    config.setdefault("max_pages_per_source", 3)
    config.setdefault("max_response_bytes", 4_000_000)
    config.setdefault("request_delay_seconds", 1.5)
    config.setdefault("stale_after_days", 30)
    config.setdefault("dealer_urls", [])
    _validate(config)
    return config


def _validate(config: dict[str, Any]) -> None:
    if config.get("state") != "CA":
        raise ValueError("Only California (CA) is supported")
    if not isinstance(config.get("home_zip"), str) or not (
        len(config["home_zip"]) == 5 and config["home_zip"].isdigit()
    ):
        raise ValueError("home_zip must be a five-digit ZIP string")
    if config["target_price"] <= 0 or config["stretch_price"] < config["target_price"]:
        raise ValueError("Invalid price limits")
    if config["run_duration_minutes"] < 0:
        raise ValueError("Run duration must be non-negative")
    if config.get("search_radius_miles") is not None and config["search_radius_miles"] <= 0:
        raise ValueError("Radius must be positive")
    if set(config["enabled_sources"]) - set(SOURCES):
        raise ValueError("Unknown source in enabled_sources")
    for key in ("model_max_calls_per_run", "model_max_input_chars", "model_max_output_tokens"):
        if not isinstance(config[key], int) or config[key] < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    if not 1 <= float(config["model_timeout_seconds"]) <= 120:
        raise ValueError("model_timeout_seconds must be between 1 and 120")
    if not str(config["model_base_url"]).startswith("https://"):
        raise ValueError("model_base_url must use HTTPS")
    if not isinstance(config.get("dealer_urls"), list):
        raise ValueError("dealer_urls must be a list")
