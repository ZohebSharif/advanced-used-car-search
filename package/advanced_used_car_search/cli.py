"""Read-only CLI entry point. No seller-contact or transaction actions exist."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as BrowserError

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


def prune_generated_outputs(
    root: Path,
    *,
    retention_days: int,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    if retention_days == 0:
        return {"removed": 0, "errors": []}
    cutoff = (time.time() if now_epoch is None else now_epoch) - retention_days * 86_400
    candidates: list[Path] = []
    for mode in ("live", "diagnostic", "fixture"):
        prefix = f"{mode}-run-"
        candidates.extend(
            path
            for path in (root / "logs").glob(f"{prefix}*.jsonl")
            if path.stem.removeprefix(prefix).isdigit()
        )
    for directory in (
        root / "evidence",
        root / "evidence" / "diagnostic",
        root / "evidence" / "fixtures",
    ):
        if directory.is_dir():
            candidates.extend(path for path in directory.iterdir() if path.name.isdigit())

    removed = 0
    errors: list[str] = []
    for path in candidates:
        if path.is_symlink():
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            elif path.is_file():
                path.unlink()
            else:
                continue
            removed += 1
        except OSError:
            errors.append(str(path.relative_to(root)))
    return {"removed": removed, "errors": sorted(errors)}


def run(args: argparse.Namespace, *, dry: bool = False, test: bool = False) -> dict[str, Any]:
    config = load(
        args.config,
        home_zip=getattr(args, "zip", None),
        search_radius_miles=getattr(args, "radius", None),
        target_price=getattr(args, "max_price", None),
        state=getattr(args, "state", None),
        run_duration_minutes=getattr(args, "duration_minutes", None),
    )
    if dry:
        config["model_enabled"] = bool(getattr(args, "model", False))
    fixtures = sorted((ROOT / "fixtures").glob("*.html")) if dry else []
    if dry and not fixtures:
        raise ValueError(
            f"No HTML fixtures found in {ROOT / 'fixtures'}. "
            "Run from the repository directory, or set ADVANCED_USED_CAR_SEARCH_HOME to it."
        )
    started, start = now(), time.monotonic()
    duration = (
        0 if dry else (min(config["run_duration_minutes"], 3) if test else config["run_duration_minutes"])
    )
    deadline = start + duration * 60
    database_path = ROOT / (
        "fixtures.sqlite3" if dry else "diagnostic.sqlite3" if test else "listings.sqlite3"
    )
    db = Store(database_path)
    run_id = db.start(started)
    mode = "fixture" if dry else "diagnostic" if test else "live"
    logpath = ROOT / "logs" / f"{mode}-run-{run_id}.jsonl"
    evidence_dir = ROOT / "evidence" / (
        f"fixtures/{run_id}" if dry else f"diagnostic/{run_id}" if test else str(run_id)
    )
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
            for fixture in fixtures:
                ingest(
                    fixture.read_text(encoding="utf-8"),
                    f"https://example.com/vehicle/{fixture.stem}",
                    "fixture",
                )
                db.event(run_id, "fixture", "fixture", fixture.name, now())
            authoritative_sources.add("fixture")
        else:
            for source in config["enabled_sources"]:
                disabled = source in POLICY_RESTRICTED and (
                    source != "dealers" or not config.get("dealer_urls")
                )
                if not disabled and time.monotonic() >= deadline:
                    detail = "time limit reached"
                    db.event(run_id, source, "deadline", detail, now())
                    if test:
                        print(f"{source}: deadline — {detail}")
                    continue
                if not disabled and browser is None:
                    browser = Browser(
                        headless=getattr(args, "headless", False),
                        max_response_bytes=int(config["max_response_bytes"]),
                    )
                    tools.browser = browser
                result = ADAPTERS[source].run(None if disabled else browser, config, deadline)
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
        output_cleanup = prune_generated_outputs(
            ROOT,
            retention_days=int(config["output_retention_days"]),
        )
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
            "output_cleanup": output_cleanup,
        }
        report = build(summary, list(current.values()), db.events(run_id), changes)
        db.finish(run_id, finished, summary)
        report_directory = ROOT / "reports" / ("fixtures" if dry else "diagnostic" if test else "")
        write(report, report_directory)
        log(logpath, event="finished", summary=summary)
        table(list(current.values()))
        print(f"Report: {report_directory / 'latest.md'} | {report_directory / 'latest.json'}")
        print(f"View again: advanced-used-car-search report --mode {mode}")
        db.close()
    return report


def CA_LOCATION(value: Any) -> bool:
    return bool(value and (", CA" in str(value) or "California" in str(value)))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="advanced-used-car-search",
        description="Read-only research for California 2022 Lexus ES 300h listings.",
        epilog=(
            "Start offline: advanced-used-car-search dry-run\n"
            "View the demo: advanced-used-car-search report --mode fixture\n"
            "Live browsing is opt-in and never contacts sellers or submits forms."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    root.add_argument(
        "--config", type=Path, default=ROOT / "config.yaml",
        help="YAML configuration path (place before the command; default: %(default)s)",
    )
    commands = root.add_subparsers(dest="command", title="commands")
    live = commands.add_parser("run", help="research live public listings (requires Chromium)")
    live.add_argument("--headless", action="store_true", help="hide the browser window")
    live.add_argument("--duration-minutes", type=float, help="maximum runtime; sources may finish earlier")
    live.add_argument("--zip", help="five-digit California search ZIP")
    live.add_argument("--radius", type=int, help="search radius in miles")
    live.add_argument("--max-price", type=int, help="target price in USD; must not exceed stretch_price")
    live.add_argument("--state", choices=["CA"], help="supported state: CA only")
    report = commands.add_parser("report", help="read a saved live, fixture, or diagnostic report")
    report.add_argument(
        "--mode", choices=["live", "fixture", "diagnostic"], default="live",
        help="report to read (default: live; use fixture after dry-run)",
    )
    report.add_argument("--format", choices=["md", "json"], default="md", help="output format (default: md)")
    commands.add_parser("listings", help="show stored live inventory, not fixture data")
    commands.add_parser("sources", help="show source policy and latest live-run status")
    dry = commands.add_parser("dry-run", help="try bundled examples offline; no browser or API key needed")
    dry.add_argument(
        "--model", action="store_true",
        help="opt in to bounded DeepSeek calls on fixtures (requires an API key; may incur cost)",
    )
    test_sources = commands.add_parser(
        "test-sources", help="diagnose live sources without changing live inventory (at most 3 minutes)"
    )
    test_sources.add_argument("--duration-minutes", type=float, help="runtime limit, capped at 3 minutes")
    test_sources.add_argument("--headless", action="store_true", help="hide the browser window")
    doctor = commands.add_parser("doctor", help="check configuration, storage, and live-browser readiness")
    doctor.add_argument(
        "--check-model", action="store_true",
        help="opt in to one bounded DeepSeek request (requires model/key; may incur cost)",
    )
    return root


def main(argv: list[str] | None = None) -> int:
    command_parser = parser()
    args = command_parser.parse_args(argv)
    if args.command is None:
        command_parser.print_help()
        return 0
    try:
        return dispatch(args)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except BrowserError:
        print(
            "Browser could not start or complete the request. Install Chromium with "
            "`uv run playwright install chromium`, then run "
            "`advanced-used-car-search doctor`. For an offline demo, use `dry-run`.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Any completed report is in the reports directory.", file=sys.stderr)
        return 130


def dispatch(args: argparse.Namespace) -> int:
    if args.command in {"run", "test-sources", "dry-run"}:
        run(args, dry=args.command == "dry-run", test=args.command == "test-sources")
        return 0
    if args.command == "doctor":
        return print_checks(run_doctor(load(args.config), ROOT, check_model=args.check_model))
    if args.command == "sources":
        config = load(args.config)
        store = Store(ROOT / "listings.sqlite3")
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
        store = Store(ROOT / "listings.sqlite3")
        entries = store.listings()
        if entries:
            table(entries)
        else:
            print(
                "No live listings saved yet. Start with `advanced-used-car-search dry-run`, "
                "then `advanced-used-car-search report --mode fixture` for sample results."
            )
        store.close()
        return 0
    directory = ROOT / "reports" / (
        "fixtures" if args.mode == "fixture" else "diagnostic" if args.mode == "diagnostic" else ""
    )
    path = directory / f"latest.{args.format}"
    if not path.is_file():
        command = {"live": "run", "fixture": "dry-run", "diagnostic": "test-sources"}[args.mode]
        print(
            f"No {args.mode} report at {path}. "
            f"Create one with `advanced-used-car-search {command}`. "
            "For an offline demo, use `dry-run` and `report --mode fixture`.",
            file=sys.stderr,
        )
        return 1
    print(path.read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
