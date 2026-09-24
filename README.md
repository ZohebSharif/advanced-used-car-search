# lexus-hunter

Local, read-only research CLI for public California listings of the 2022 Lexus ES 300h. It extracts visible listing evidence, applies deterministic hard gates, ranks candidates, tracks price and relisting history, and writes local reports. It cannot log in, submit forms, contact sellers, place orders, make payments, or bypass access controls.

Results are research leads. Seller/source title, history, CPO, and vehicle claims are not independently verified.

## Setup

Requirements: macOS or Linux and [`uv`](https://docs.astral.sh/uv/). The project pins Python 3.12 because its editable package path remains active after repeated syncs on supported macOS environments; `uv` can provision it.

```sh
cd lexus-hunter
./setup.sh
uv run lexus-hunter doctor
```

`setup.sh` creates `.env` only when absent, installs the locked Python environment and Playwright Chromium, creates local output directories, and runs the deterministic fixture smoke test. Re-running it preserves an existing `.env`.

## Optional DeepSeek model assistance

Deterministic extraction and ranking are the default and require no API key. Optional model assistance uses DeepSeek's OpenAI-compatible JSON API only for bounded suggestions from visible page text.

Edit `.env`:

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
MODEL_ENABLED=false
MODEL_MAX_CALLS_PER_RUN=3
MODEL_MAX_INPUT_CHARS=12000
MODEL_TIMEOUT_SECONDS=20
```

Set `MODEL_ENABLED=true` and provide `DEEPSEEK_API_KEY` to enable it. Environment variables override `config.yaml`. The API key is read only from the environment and is never written to logs, evidence, SQLite, or reports.

The model is advisory. It cannot override deterministic year, make/model, URL, price, mileage, location, VIN, title, or history evidence. Invalid JSON, schema violations, timeouts, and provider errors are isolated; the deterministic pipeline continues. Every run reports the model name, enabled state, bounded call count, latency, input size, and call status.

`doctor` does not make a model request. One explicit bounded connectivity check:

```sh
uv run lexus-hunter doctor --check-model
```

The real-provider pytest is opt-in and otherwise skipped:

```sh
RUN_DEEPSEEK_INTEGRATION=1 uv run python -m pytest -q -k real_deepseek_json_mode_opt_in
```

## Commands

```sh
# Deterministic offline fixture path; no network or model call
uv run lexus-hunter dry-run

# Optional bounded model-assisted fixture path
uv run lexus-hunter dry-run --model

# Short source diagnostic; does not age or stale stored live inventory
uv run lexus-hunter test-sources --headless --duration-minutes 3

# Bounded live read-only run
uv run lexus-hunter run --headless --duration-minutes 45

# Optional live constraints
uv run lexus-hunter run --zip 95112 --radius 250 --max-price 30000 --state CA

uv run lexus-hunter report
uv run lexus-hunter listings
uv run lexus-hunter sources
uv run lexus-hunter doctor
```

A live run stops when enabled adapters finish; it does not idle until the full deadline. `test-sources` is deliberately diagnostic, not exhaustive. Status `empty` means a public search page loaded but no concrete vehicle-detail URL was identified; it is not proof that the source has no matching inventory. `blocked`, `failed`, `disabled`, and `deadline` are never reported as successful searches.

The default daily source order is Lexus inventory first, explicitly configured California dealer inventory second, then the remaining enabled public sources. A custom `enabled_sources` list is dispatched in the supplied order. Dealers remain disabled and receive no browser when `dealer_urls` is empty.

Facebook Marketplace, Cars.com, Craigslist, generic search-engine discovery, and dealers without explicitly configured public URLs are policy-disabled. Public access restrictions, authentication requirements, CAPTCHA, HTTP 401/403/429, private/non-global addresses, unsafe ports, and disallowed redirects fail closed without bypass or retry loops.

Lexus L/Certified discovery uses the public inventory UI: it selects ES Hybrid, the configured target year, and the smallest supported distance that covers the requested radius, then opens each visible `VIEW DETAILS` overlay. The original requested radius remains the acceptance boundary: every accepted overlay must visibly report `MILES AWAY` at or below it. Lexus supports at most 500 miles; requests above 500 are searched at 500 and explicitly reported as incomplete coverage. The scanner stores a candidate only after that detail overlay supplies model, price, and mileage evidence.

To include a California dealer, add its public inventory search URL explicitly in `config.yaml`:

```yaml
dealer_urls:
  - https://www.tustinlexus.com/used-vehicles/certified-pre-owned-vehicles/
```

Dealer search pages are treated only as discovery surfaces. A dealer candidate must have a concrete VIN-bearing or known vehicle-detail URL, and its detail page must visibly contain one vehicle's VIN, target model/year, price, and mileage. Login, CAPTCHA, lead forms, `CONTACT DEALER`, and transaction controls are never used.

## Local data and lifecycle

- `hunter.sqlite3`: authoritative live runs, identities, observations, source events, and schema version.
- `diagnostic.sqlite3`: isolated source-diagnostic runs that cannot alter live inventory lifecycle state.
- `fixtures.sqlite3`: isolated fixture runs.
- `evidence/<run>/`, `evidence/diagnostic/<run>/`, and `evidence/fixtures/<run>/`: bounded HTML plus visible-text evidence.
- `logs/live-run-*.jsonl`, `logs/diagnostic-run-*.jsonl`, and `logs/fixture-run-*.jsonl`: structured local run events.
- `reports/latest.{json,md}`: latest real live run.
- `reports/diagnostic/latest.{json,md}`: latest source diagnostic.
- `reports/fixtures/latest.{json,md}`: latest fixture dry run.

Identity preference: VIN, then canonical detail URL, then a conservative seller/vehicle fallback. A listing records `first_seen`, `last_seen`, `last_seen_run`, `missing_runs`, `reappeared_at`, `relisted_count`, and `stale`. Only a completed authoritative live source scan may increment missing inventory. Blocked, disabled, failed, deadline, empty, and diagnostic source tests do not age listings. Reappearance resets `missing_runs` and records a relisting event while preserving price observations.

Clean-title text is a seller/source claim, not verification. Negated language such as “no salvage title,” “not rebuilt,” and “no major accident” is kept separate from affirmative adverse evidence. Missing or unavailable VIN validation remains a manual-verification flag; invalid or mismatched VIN evidence is a hard exclusion.

## Local quality gates

Run the local quality and dependency gates:

```sh
uv sync --locked --extra dev
uv lock --check
uv pip check
uv run pip-audit
uv run python -c 'import lexus_hunter.config; print(lexus_hunter.config.__file__); print(lexus_hunter.config.DEFAULTS["max_response_bytes"])'
uv run lexus-hunter dry-run
uv run python -m pytest -q
uv run ruff check .
uv run mypy package/lexus_hunter
bash -n setup.sh
```

The editable-package check must print a path under `package/lexus_hunter/` followed by
`4000000`. `uv lock --check` verifies that `uv.lock` matches the project metadata, and
`uv pip check` verifies that the installed environment has compatible dependencies.
These are integrity checks, not vulnerability checks. `uv run pip-audit` audits the
installed locked environment for known vulnerabilities using public advisory data.
It may query the PyPI JSON API, requires no credentials or secrets, and never accesses
listing or dealer sites.

## Continuous integration

`.github/workflows/ci.yml` runs one bounded `Quality and dependency audit` job for pull requests
and pushes to `main`. Superseded runs on the same ref are cancelled. The workflow grants
the GitHub token only `contents: read`, disables persisted checkout credentials, pins
third-party actions and the uv/Python toolchain, and caches packages using `uv.lock`.

CI sets `MODEL_ENABLED=false` and runs only the deterministic fixture scanner command
`uv run lexus-hunter dry-run`; it never runs `lexus-hunter run`, `test-sources`, or
`setup.sh`. It does not load `.env`, use API keys, install a browser, or access live
marketplace/dealer pages. The advisory audit may query the public PyPI vulnerability
service, but it uses no credentials and sends no listing or user data.

The only uploaded artifacts are the generated fixture `latest.md` and `latest.json`
reports, retained for seven days. Local SQLite databases, logs, HTML/text evidence,
`.env`, and all live or diagnostic reports are excluded.

The test suite validates these workflow boundaries, including triggers, permissions,
action pinning, the locked advisory-audit dependency, required commands, prohibited
live commands, and the fixture-only artifact allowlist.

The offline suite covers extraction, negation-aware title evidence, deterministic hard gates, model JSON validation and call caps, SSRF controls, pre-request non-public DNS rejection, detail-URL filtering, deduplication, price history, schema migration, relisting/stale transitions, diagnostic-run isolation, and report provenance. The direct HTTP tool additionally validates the connected response peer; Playwright cannot independently verify Chromium's connected peer after its pre-request DNS check. The DeepSeek network test remains opt-in so ordinary tests never consume API quota.
