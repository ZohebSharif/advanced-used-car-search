"""Read-only CLI entry point. No seller-contact or transaction actions exist."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import ROOT, SOURCES, load
from .doctor import print_checks, run_doctor
from .extract import extract
from .model import DeepSeekClient, ModelBudget
from .rank import evaluate, validate_vin
from .report import build, table, write
from .sources import ADAPTERS, POLICY_RESTRICTED, Browser
from .store import Store
from .tools import AgentTools


def now() -> str:
    return datetime.now(UTC).isoformat()


def log(path: Path, **data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"time": now(), **data}, sort_keys=True) + "\n")


def run(args: argparse.Namespace, *, dry: bool = False, test: bool = False) -> dict[str, Any]:
    config = load(
        args.config,
        home_zip=getattr(args, "zip", None),
        search_radius_miles=getattr(args, "radius", None),
        target_price=getattr(args, "max_price", None),
        state=getattr(args, "state", None),
        run_duration_minutes=getattr(args, "duration_minutes", None),
    )
    if dry and bool(getattr(args, "model", False)):
        config["model_enabled"] = True
    started, start = now(), time.monotonic()
    duration = (
        0 if dry else (min(config["run_duration_minutes"], 3) if test else config["run_duration_minutes"])
    )
    deadline = start + duration * 60
    database_path = ROOT / ("fixtures.sqlite3" if dry else "hunter.sqlite3")
    db = Store(database_path)
    run_id = db.start(started)
    mode = "fixture" if dry else "live"
    logpath = ROOT / "logs" / f"{mode}-run-{run_id}.jsonl"
    evidence_dir = ROOT / "evidence" / (f"fixtures/{run_id}" if dry else str(run_id))
    counts = {"discovered": 0, "deduplicated": 0, "excluded": 0, "ranked": 0}
    changes: dict[str, list[dict[str, Any]]] = {"new": [], "drops": [], "relisted": [], "stale": []}
    current: dict[str, dict[str, Any]] = {}
    searched: list[str] = []
    authoritative_sources: set[str] = set()
    model_client = DeepSeekClient(config)
    model_budget = ModelBudget(model_client, int(config["model_max_calls_per_run"]))
    use_model = not dry or bool(getattr(args, "model", False))
    browser: Browser | None = None
    tools = AgentTools(config, db, evidence_dir, model_budget=model_budget, run_id=run_id)
    evidence_numbers: dict[str, int] = {}

    def ingest(html: str, url: str, source: str) -> None:
        if source != "fixture" and not ADAPTERS[source].is_detail(url):
            raise ValueError("candidate URL is not a concrete vehicle-detail URL")
        evidence_numbers[source] = evidence_numbers.get(source, 0) + 1
        evidence = tools.save_listing_evidence(source, evidence_numbers[source], html)
        entry = extract(html, url, source, config, use_model=use_model, model_budget=model_budget)
        entry["evidence_paths"] = evidence
        counts["discovered"] += 1
        status = (
            validate_vin(entry["vin"], remote=not dry, year=entry["year"] or config["target_year"])
            if entry["vin"]
            else "missing"
        )
        comparable = [
            item["price"]
            for item in db.listings()
            if item.get("price") and item.get("year") == 2022 and CA_LOCATION(item.get("location"))
        ]
        entry = evaluate(entry, config, comparable, vin_status=status)
        if entry["category"] == "avoid":
            counts["excluded"] += 1
        else:
            counts["ranked"] += 1
        identity = db.identity(entry)
        if identity in current:
            counts["deduplicated"] += 1
        change = db.save(entry, run_id)
        current[identity] = entry
        if change["new"] and entry["category"] != "avoid":
            changes["new"].append(entry)
        if change["price_drop"] and entry["category"] != "avoid":
            changes["drops"].append(entry)
        if change["relisted"]:
            changes["relisted"].append(entry)
        log(
            logpath,
            event="candidate",
            source=source,
            url=entry["url"],
            category=entry["category"],
            title_state=entry["title_state"],
            model_status=entry["model_status"],
            flags=entry["flags"],
        )

    try:
        if dry:
            for fixture in sorted((ROOT / "fixtures").glob("*.html")):
                ingest(
                    fixture.read_text(encoding="utf-8"),
                    f"https://example.com/vehicle/{fixture.stem}",
                    "fixture",
                )
                db.event(run_id, "fixture", "fixture", fixture.name, now())
            authoritative_sources.add("fixture")
        else:
            for source in config["enabled_sources"]:
                if time.monotonic() >= deadline:
                    db.event(run_id, source, "deadline", "time limit reached", now())
                    continue
                disabled = source in POLICY_RESTRICTED and (
                    source != "dealers" or not config.get("dealer_urls")
                )
                if not disabled and browser is None:
                    browser = Browser(
                        headless=getattr(args, "headless", False),
                        max_response_bytes=int(config["max_response_bytes"]),
                    )
                    tools.browser = browser
                result = ADAPTERS[source].run(browser, config, deadline)
                source_complete = result.complete
                try:
                    for search_url, search_html in result.search_pages:
                        evidence_numbers[source] = evidence_numbers.get(source, 0) + 1
                        tools.save_listing_evidence(source, evidence_numbers[source], search_html)
                        log(logpath, event="search_evidence", source=source, url=search_url)
                    for url, html in result.listings:
                        try:
                            ingest(html, url, source)
                        except Exception as exc:
                            source_complete = False
                            log(
                                logpath,
                                event="parse_error",
                                source=source,
                                url=url,
                                error=type(exc).__name__,
                            )
                            db.event(
                                run_id,
                                source,
                                "failed",
                                f"detail parse failed: {type(exc).__name__}",
                                now(),
                            )
                except Exception as exc:
                    source_complete = False
                    log(logpath, event="evidence_error", source=source, error=type(exc).__name__)
                if source_complete and result.status in {"ok", "empty"}:
                    searched.append(source)
                if not test and source_complete and result.status == "ok":
                    authoritative_sources.add(source)
                detail = (
                    f"{result.detail}; searches={result.completed_searches}; retries={result.retries}; "
                    f"parsed={len(result.listings)}; complete={source_complete}"
                )
                event_status = (
                    result.status
                    if source_complete or result.status not in {"ok", "empty"}
                    else "failed"
                )
                db.event(run_id, source, event_status, detail, now())
                if test:
                    print(f"{source}: {event_status} — {detail}")
        if not test:
            db.finalize_inventory(run_id, int(config["stale_after_days"]), authoritative_sources)
        changes["stale"] = [item for item in db.listings() if item.get("stale")]
    finally:
        if browser:
            browser.close()
        finished = now()
        model_usage = model_budget.summary()
        summary = {
            "run_id": run_id,
            "mode": "dry-run fixtures" if dry else "source diagnostic" if test else "live browser",
            "started": started,
            "finished": finished,
            "duration_seconds": round(time.monotonic() - start, 2),
            "sources_searched": searched,
            "model_calls": model_usage["calls"],
            "model_usage": model_usage,
            **counts,
        }
        report = build(summary, list(current.values()), db.events(run_id), changes)
        db.finish(run_id, finished, summary)
        report_directory = ROOT / "reports" / ("fixtures" if dry else "diagnostic" if test else "")
        write(report, report_directory)
        log(logpath, event="finished", summary=summary)
        table(list(current.values()))
        print(f"Report: {report_directory / 'latest.md'} | {report_directory / 'latest.json'}")
        db.close()
    return report


def CA_LOCATION(value: Any) -> bool:
    return bool(value and (", CA" in str(value) or "California" in str(value)))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="lexus-hunter", description="Read-only Lexus ES 300h listing research"
    )
    root.add_argument("--config", type=Path, default=ROOT / "config.yaml", help="YAML configuration path")
    commands = root.add_subparsers(dest="command", required=True)
    live = commands.add_parser("run")
    live.add_argument("--headless", action="store_true")
    live.add_argument("--duration-minutes", type=float)
    live.add_argument("--zip")
    live.add_argument("--radius", type=int)
    live.add_argument("--max-price", type=int)
    live.add_argument("--state")
    for command in ("report", "listings", "sources"):
        commands.add_parser(command)
    dry = commands.add_parser("dry-run")
    dry.add_argument("--model", action="store_true", help="use configured bounded model on fixtures")
    test_sources = commands.add_parser("test-sources")
    test_sources.add_argument("--duration-minutes", type=float)
    test_sources.add_argument("--headless", action="store_true")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--check-model", action="store_true", help="make one explicit bounded DeepSeek call")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command in {"run", "test-sources", "dry-run"}:
        run(args, dry=args.command == "dry-run", test=args.command == "test-sources")
        return 0
    if args.command == "doctor":
        try:
            config = load(args.config)
        except Exception as exc:
            print(f"configuration  fail     {type(exc).__name__}: {exc}")
            return 1
        return print_checks(run_doctor(config, ROOT, check_model=args.check_model))
    if args.command == "sources":
        config = load(args.config)
        store = Store(ROOT / "hunter.sqlite3")
        latest = store.latest()
        events = store.events(latest["id"]) if latest else []
        for name in SOURCES:
            match = next((event for event in reversed(events) if event["source"] == name), None)
            configured = "enabled" if name in config["enabled_sources"] else "disabled"
            policy = (
                "policy-disabled"
                if name in POLICY_RESTRICTED and not (name == "dealers" and config["dealer_urls"])
                else configured
            )
            status = match["status"] if match else "not tested"
            detail = match["detail"] if match else ""
            print(f"{name:<13} {policy:<16} {status} {detail}")
        store.close()
        return 0
    if args.command == "listings":
        store = Store(ROOT / "hunter.sqlite3")
        table(store.listings())
        store.close()
        return 0
    path = ROOT / "reports" / "latest.md"
    message = path.read_text(encoding="utf-8") if path.exists() else (
        "No live report yet. Run `lexus-hunter run` first."
    )
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
