from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def load_workflow() -> tuple[dict, str]:
    text = WORKFLOW_PATH.read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert isinstance(workflow, dict)
    return workflow, text


def test_ci_has_bounded_triggers_permissions_and_concurrency() -> None:
    workflow, _ = load_workflow()

    assert set(workflow["on"]) == {"pull_request", "push"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "true"
    assert workflow["env"]["MODEL_ENABLED"] == "false"
    assert set(workflow["jobs"]) == {"quality"}
    assert workflow["jobs"]["quality"]["timeout-minutes"] == "15"
    assert workflow["jobs"]["quality"]["name"] == "Quality and dependency audit"


def test_ci_runs_only_offline_scanner_and_required_quality_gates() -> None:
    workflow, text = load_workflow()
    steps = workflow["jobs"]["quality"]["steps"]
    commands = "\n".join(step["run"] for step in steps if "run" in step)

    assert "uv sync --locked --extra dev" in commands
    assert (
        "uv run python -c 'import lexus_hunter.config; "
        "print(lexus_hunter.config.__file__); "
        'print(lexus_hunter.config.DEFAULTS["max_response_bytes"])\''
    ) in commands
    assert "installed != source" in commands
    assert "installed.read_bytes() == source.read_bytes()" in commands
    assert 'config.DEFAULTS["max_response_bytes"] == 4000000' in commands
    assert "uv run lexus-hunter dry-run" in commands
    assert "uv run python -m pytest -q" in commands
    assert "uv run ruff check ." in commands
    assert "uv run mypy package/lexus_hunter" in commands
    assert "bash -n setup.sh" in commands
    assert "uv lock --check" in commands
    assert "uv pip check" in commands
    assert "uv run pip-audit" in commands

    assert not re.search(r"\buv run lexus-hunter (?:run|test-sources)\b", commands)
    assert "./setup.sh" not in commands
    assert "${{ secrets." not in text
    assert "DEEPSEEK_API_KEY" not in text


def test_advisory_audit_is_a_locked_development_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    development_dependencies = project["project"]["optional-dependencies"]["dev"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())

    assert "pip-audit>=2.10,<3" in development_dependencies
    assert any(package["name"] == "pip-audit" for package in lock["package"])


def test_local_package_is_noneditable_with_source_freshness_tracking() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package = tomllib.loads((ROOT / "package" / "pyproject.toml").read_text())

    assert project["tool"]["uv"]["sources"]["lexus-hunter"] == {
        "path": "package",
        "editable": False,
    }
    assert package["tool"]["uv"]["cache-keys"] == [
        {"file": "pyproject.toml"},
        {"file": "lexus_hunter/**/*.py"},
    ]

def test_ci_pins_actions_and_uploads_only_fixture_reports() -> None:
    workflow, _ = load_workflow()
    steps = workflow["jobs"]["quality"]["steps"]
    actions = [step["uses"] for step in steps if "uses" in step]

    assert actions
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action) for action in actions)

    upload = next(step for step in steps if step.get("name") == "Preserve deterministic fixture reports")
    artifact_paths = set(upload["with"]["path"].splitlines())
    assert artifact_paths == {
        "reports/fixtures/latest.md",
        "reports/fixtures/latest.json",
    }
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] == "7"
