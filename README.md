# advanced-used-car-search

Local, read-only research CLI for public California listings of the 2022 Lexus ES 300h. It extracts visible listing evidence, applies deterministic hard gates, ranks candidates, tracks price and relisting history, and writes local reports. It cannot log in, submit forms, contact sellers, place orders, make payments, or bypass access controls.

Results are research leads. Seller/source title, history, CPO, and vehicle claims are not independently verified.

**Current scope:** this is a focused search tool, not a general-purpose vehicle marketplace.
The adapters and ranking rules target **2022 Lexus ES 300h listings in California**.
You can adjust ZIP, radius, budget, mileage, colors, and enabled sources in `config.yaml`;
changing a year or model setting does not make other vehicles supported.

## Quick start: an offline demo

Requirements: macOS or Linux and [`uv`](https://docs.astral.sh/uv/). The project pins
Python 3.12; `uv` can install it for you. After cloning or downloading this repository:

```sh
cd advanced-used-car-search
./setup.sh
uv run advanced-used-car-search report --mode fixture
```

Setup downloads locked Python dependencies, creates `.env` only if absent, and runs
the bundled HTML examples. **The demo itself makes no network or model requests and
needs no browser, account, or API key.** An initial dependency/interpreter download
does require internet access. Setup does not install Chromium unless requested.
Re-running setup preserves your `.env` and existing local data.

The demo processes four sample listings, prints the ranked results, and saves:

- Human-readable report: `reports/fixtures/latest.md`
- Machine-readable report: `reports/fixtures/latest.json`

These are **sample results, not current listings**. To run the demo again:
`uv run advanced-used-car-search dry-run`. To see command help:
`uv run advanced-used-car-search` or append `--help` to a command.

## When you are ready for live research

Review `config.yaml` first. The defaults search from ZIP `95112`, within 250 miles,
with a $30,000 target price, $32,000 stretch price, and 60,000-mile limit.
Keep ZIP codes quoted and keep `state: CA`. The CLI's `--max-price` changes the target,
not the stretch ceiling; raise `stretch_price` in the file if needed.

```sh
./setup.sh --browser
uv run advanced-used-car-search doctor
uv run advanced-used-car-search test-sources --headless --duration-minutes 3
uv run advanced-used-car-search report --mode diagnostic
uv run advanced-used-car-search run --headless --duration-minutes 45
uv run advanced-used-car-search report
```

The diagnostic and live commands access public websites and may return blocked or
empty results. They never contact sellers, fill forms, log in, or buy anything.
`doctor` checks local readiness and launches Chromium without navigating to a
marketplace; it makes no model request unless you pass `--check-model`.

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
uv run advanced-used-car-search doctor --check-model
```

The real-provider pytest is opt-in and otherwise skipped:

```sh
RUN_DEEPSEEK_INTEGRATION=1 uv run python -m pytest -q -k real_deepseek_json_mode_opt_in
```

## Commands

```sh
# Deterministic offline fixture path; no network or model call
uv run advanced-used-car-search dry-run

# Optional bounded model-assisted fixture path
uv run advanced-used-car-search dry-run --model

# Short source diagnostic; does not age or stale stored live inventory
uv run advanced-used-car-search test-sources --headless --duration-minutes 3

# Bounded live read-only run
uv run advanced-used-car-search run --headless --duration-minutes 45

# Optional live constraints
uv run advanced-used-car-search run --zip 95112 --radius 250 --max-price 30000 --state CA

uv run advanced-used-car-search report
uv run advanced-used-car-search report --mode fixture
uv run advanced-used-car-search report --mode diagnostic --format json
uv run advanced-used-car-search listings
uv run advanced-used-car-search sources
uv run advanced-used-car-search doctor
```

A live run stops when enabled adapters finish; it does not idle until the full deadline. `test-sources` is deliberately diagnostic, not exhaustive. Status `empty` means a public search page loaded but no concrete vehicle-detail URL was identified; it is not proof that the source has no matching inventory. `blocked`, `failed`, `disabled`, and `deadline` are never reported as successful searches.

The default source order is Lexus L/Certified, the explicitly configured Lexus
Stevens Creek certified inventory, then AutoTrader. This is a starting configuration,
not a guarantee of current availability: public endpoints and access policies change.
A custom `enabled_sources` list is dispatched in the supplied order. Dealers remain
disabled and receive no browser when `dealer_urls` is empty.

Facebook Marketplace, Cars.com, Craigslist, generic search-engine discovery, and dealers
without explicitly configured public URLs are policy-disabled. CarGurus, TrueCar, and Edmunds
are not enabled after access-control failures in prior diagnostics. Public access
restrictions, authentication requirements, CAPTCHA, HTTP 401/403/429, private/non-global
addresses, unsafe ports, and disallowed redirects fail closed without bypass or retry loops.

The checked-in dealer URL is the public certified inventory page for Lexus Stevens Creek:

```yaml
dealer_urls:
  - https://www.lexusstevenscreek.com/certified-pre-owned.html
```

Dealer search pages are treated only as discovery surfaces. A dealer candidate must have a concrete VIN-bearing or known vehicle-detail URL, and its detail page must visibly contain one vehicle's VIN, target model/year, price, and mileage. Login, CAPTCHA, lead forms, `CONTACT DEALER`, and transaction controls are never used.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `uv` not found | Install it using the linked uv instructions, reopen your terminal, and rerun setup. |
| Configuration not found | Run commands from this repository. A custom file goes before the command: `uv run advanced-used-car-search --config /path/to/config.yaml dry-run`. |
| Browser check fails | Run `uv run playwright install chromium`, then `doctor`. On Linux, Playwright may also need system libraries; follow its installation error. The offline demo still works without a browser. |
| No live report after the demo | Use `report --mode fixture`. Plain `report` intentionally reads only live results. |
| No diagnostic results in `sources` | `sources` shows the latest live-run status. Read `report --mode diagnostic` for source-test results. |
| Source blocked or no candidates | Review its report status. Do not bypass access controls; a blocked or empty source is not evidence that no matching cars exist. |
| Model unavailable | Leave it disabled for normal use. For explicit model calls, configure `.env` as described above. |

Commands return `0` on completion, `1` for a local configuration/storage/browser error
or a missing report, `2` for invalid command syntax, and `130` when interrupted.
A completed scan may still contain failed or blocked sources; inspect the source
statuses in its report rather than treating exit status alone as search success.

Data normally stays in the repository directory. To run the installed command from
elsewhere, set `ADVANCED_USED_CAR_SEARCH_HOME` to the absolute repository path.
`--config` selects settings only; it does not move databases, fixtures, or reports.
Do not publish `.env`, SQLite databases, logs, or evidence directories.

## Local data and lifecycle

### Upgrading an existing checkout

The previous `lexus-hunter` command and `LEXUS_HUNTER_HOME` variable have been
replaced by `advanced-used-car-search` and `ADVANCED_USED_CAR_SEARCH_HOME`.
Rerun setup and update scripts that invoke the old command.

Existing live history is not automatically imported from `hunter.sqlite3`.
Before running the new CLI, stop all old processes, retain a backup, and use the
SQLite CLI to copy the database consistently (including any committed WAL data):

```sh
test ! -e listings.sqlite3 && sqlite3 hunter.sqlite3 ".backup listings.sqlite3"
```

Only run this when `hunter.sqlite3` exists. If `listings.sqlite3` already exists,
do not overwrite it; keep both databases until you decide which history to use.
Reports, evidence, and diagnostic/fixture database locations are unchanged.

- `listings.sqlite3`: authoritative live runs, identities, observations, source events, and schema version.
- `diagnostic.sqlite3`: isolated source-diagnostic runs that cannot alter live inventory lifecycle state.
- `fixtures.sqlite3`: isolated fixture runs.
- `evidence/<run>/`, `evidence/diagnostic/<run>/`, and `evidence/fixtures/<run>/`: bounded HTML plus visible-text evidence.
- `logs/live-run-*.jsonl`, `logs/diagnostic-run-*.jsonl`, and `logs/fixture-run-*.jsonl`: structured local run events.
- `reports/latest.{json,md}`: latest real live run.
- `reports/diagnostic/latest.{json,md}`: latest source diagnostic.
- `reports/fixtures/latest.{json,md}`: latest fixture dry run.

Identity preference: VIN, then canonical detail URL, then a conservative seller/vehicle fallback. A listing records `first_seen`, `last_seen`, `last_seen_run`, `missing_runs`, `reappeared_at`, `relisted_count`, and `stale`. Only a completed authoritative live source scan may increment missing inventory. Blocked, disabled, failed, deadline, empty, and diagnostic source tests do not age listings. Reappearance resets `missing_runs` and records a relisting event while preserving price observations.

Generated run logs and per-run evidence directories older than `output_retention_days` are
removed after each scan completes. The default is 30 days; `0` disables cleanup. SQLite
history and the latest reports are never removed by this cleanup.

Clean-title text is a seller/source claim, not verification. Negated language such as “no salvage title,” “not rebuilt,” and “no major accident” is kept separate from affirmative adverse evidence. Missing or unavailable VIN validation remains a manual-verification flag; invalid or mismatched VIN evidence is a hard exclusion.

## Local quality gates

Run the local quality and dependency gates:

```sh
uv sync --locked --extra dev
uv lock --check
uv pip check
uv run pip-audit
uv run python -c 'import advanced_used_car_search.config; print(advanced_used_car_search.config.__file__); print(advanced_used_car_search.config.DEFAULTS["max_response_bytes"])'
uv run python -c 'from pathlib import Path; import advanced_used_car_search.config as config; source=(Path.cwd() / "package" / "advanced_used_car_search" / "config.py").resolve(); installed=Path(config.__file__).resolve(); assert installed != source; assert installed.read_bytes() == source.read_bytes()'
uv run advanced-used-car-search dry-run
uv run python -m pytest -q
uv run ruff check .
uv run mypy package/advanced_used_car_search
bash -n setup.sh
```

The location fallback separates recognized mileage fields from city text, even
without punctuation, and stops at sentence boundaries. Structured address evidence
still takes precedence.
Mileage extraction excludes distance-to-seller labels such as `22 mi away`; only
odometer evidence is used for vehicle mileage.

The installed-package checks must print a path under the active environment's
`site-packages/advanced_used_car_search/`, followed by `4000000`, and prove that the installed `config.py`
is byte-identical to the current source. `uv lock --check` verifies that `uv.lock` matches the
project metadata, and `uv pip check` verifies that the installed environment has compatible
dependencies.
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
`uv run advanced-used-car-search dry-run`; it never runs `advanced-used-car-search run`, `test-sources`, or
`setup.sh`. It does not load `.env`, use API keys, install a browser, or access live
marketplace/dealer pages. The advisory audit may query the public PyPI vulnerability
service, but it uses no credentials and sends no listing or user data.

The only uploaded artifacts are the generated fixture `latest.md` and `latest.json`
reports, retained for seven days. Local SQLite databases, logs, HTML/text evidence,
`.env`, and all live or diagnostic reports are excluded.

The test suite validates workflow safety boundaries, including triggers, permissions,
action pinning, prohibited live commands, and the fixture-only artifact allowlist.

The offline suite covers extraction, negation-aware title evidence, deterministic hard gates, model JSON validation and call caps, SSRF controls, pre-request non-public DNS rejection, detail-URL filtering, deduplication, price history, schema migration, relisting/stale transitions, diagnostic-run isolation, and report provenance. The direct HTTP tool additionally validates the connected response peer; Playwright cannot independently verify Chromium's connected peer after its pre-request DNS check. The DeepSeek network test remains opt-in so ordinary tests never consume API quota.

## License

No license has been granted in this repository. Public visibility alone does not
grant permission to reuse, modify, or redistribute the code beyond applicable law
and GitHub's terms. Contact the owner for permission.
