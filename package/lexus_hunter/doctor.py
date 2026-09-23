from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import DeepSeekClient
from .sources import POLICY_RESTRICTED, Browser
from .store import SCHEMA_VERSION, Store


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    actionable: bool = False


def _writable(directory: Path) -> Check:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".doctor-", delete=True):
            pass
        return Check(directory.name, "ok", str(directory))
    except OSError as exc:
        return Check(directory.name, "fail", f"not writable: {exc}", True)


def run_doctor(config: dict[str, Any], root: Path, *, check_model: bool = False) -> list[Check]:
    checks: list[Check] = [Check("configuration", "ok", "config.yaml is valid")]
    for directory in (root / "logs", root / "evidence", root / "reports"):
        checks.append(_writable(directory))
    try:
        store = Store(root / "hunter.sqlite3")
        version = store.schema_version()
        store.close()
        checks.append(
            Check(
                "database",
                "ok" if version == SCHEMA_VERSION else "fail",
                f"writable schema version {version}",
                version != SCHEMA_VERSION,
            )
        )
    except Exception as exc:
        checks.append(Check("database", "fail", f"{type(exc).__name__}: {exc}", True))
    try:
        browser = Browser(headless=True, max_response_bytes=int(config["max_response_bytes"]))
        browser.close()
        checks.append(Check("browser", "ok", "Playwright Chromium launches"))
    except Exception as exc:
        checks.append(Check("browser", "fail", f"{type(exc).__name__}: {exc}", True))
    enabled = list(config["enabled_sources"])
    active = [name for name in enabled if name not in POLICY_RESTRICTED]
    checks.append(
        Check(
            "sources",
            "ok" if active else "fail",
            f"enabled={','.join(enabled)}; directly testable={','.join(active) or 'none'}",
            not active,
        )
    )
    model = DeepSeekClient(config)
    if check_model:
        if not model.available:
            checks.append(
                Check("deepseek", "fail", "explicit check requested but model/key is unavailable", True)
            )
        else:
            suggestion, event = model.extract(
                "Visible fixture: 2022 Lexus ES 300h, $29,000, 30,000 miles, San Jose CA.",
                "https://example.invalid/vehicle/fixture",
            )
            checks.append(
                Check(
                    "deepseek",
                    "ok" if suggestion is not None else "fail",
                    f"model={event.model}; status={event.status}; latency_ms={event.latency_ms}",
                    suggestion is None,
                )
            )
    else:
        checks.append(
            Check(
                "deepseek",
                "optional",
                "connectivity not tested; pass --check-model to make one bounded call",
            )
        )
    return checks


def print_checks(checks: list[Check]) -> int:
    for check in checks:
        print(f"{check.name:<14} {check.status:<8} {check.detail}")
    return 1 if any(check.actionable for check in checks) else 0
