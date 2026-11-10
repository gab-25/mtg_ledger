# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

MTG Ledger server: a Django 6 + Django REST Framework app (Python 3.14, managed with `uv`) that holds a Magic: The
Gathering collection and manages CardTrader sales. It exposes only a JSON API under `/api/`; the interface is a
separate React SPA ([mtg_ledger_ui](https://github.com/gab-25/mtg_ledger_ui)), whose built `index.html` Django serves
for every other URL (`config/views.py`, catch-all route last in `config/urls.py`, files from `UI_APP_DIR`).

## Commands

```bash
docker compose up -d db                          # PostgreSQL (required, also for tests)
uv sync
uv run python manage.py migrate
uv run python manage.py runserver                # pages need a UI build: UI_APP_DIR=../mtg_ledger_ui/dist

uv run python manage.py test                                      # all tests
uv run python manage.py test ledger.test_scan_api                 # one module
uv run python manage.py test ledger.test_scan_api.SchemaTest.test_docs_are_public   # one test

uv run python manage.py check                                     # CI also runs these two
uv run python manage.py makemigrations --check --dry-run

uv run python manage.py spectacular --validate --fail-on-warn --file /dev/null   # OpenAPI schema check

docker compose up -d redis && uv run celery -A config worker --loglevel=info     # Cardmarket sync worker
uv run python manage.py sync_cardmarket          # queue the nightly catalog + price sync
uv run python manage.py sync_catalog [--set CODE | --all]
```

There is no linter or formatter configured. Settings come only from the environment / `.env` (see `.env.example`);
`OPENROUTER_API_KEY` and `OPENROUTER_MODEL` deliberately have no defaults in code.

## Architecture

- **Apps**: `config/` (settings, root URLs, the UI view), `accounts/` (auth and profile API in `accounts/api.py`),
  `ledger/` (the domain). Views in `ledger/api/*.py` and `accounts/api.py` are function-based DRF `@api_view`s, one
  module per UI page; business logic lives in `ledger/services.py` (CardTrader sync, publish, orders, CSV) and
  `ledger/scan.py`, not in the views.
- **Multi-user ownership**: every personal row (collection items, listings, orders, scan sessions, conflicts, logs)
  has an owner, reached through `owned_manager(path)` / `OwnedQuerySet.owned_by(user)` in `ledger/models.py`. Views
  must always start from `Model.objects.owned_by(request.user)` so another user's object answers 404
  (`ledger/test_ownership.py`). `SyncLog.objects.visible_to(user)` adds the global entries. The card catalog
  (`Expansion`, `Blueprint`) and prices are shared.
- **CardTrader tokens**: each user has their own token on a `SyncService` row (`services.get_cardtrader(user)`);
  `services.user_client(user)` never falls back to another user's token. The *service* token (catalog, prices,
  scan recognition) is the first superuser's own token (`services.service_client()`). All clients are built by
  `services.make_client`, which tests patch.
- **Cards vs printings**: `Card` is the Ledger's own card row (name + set + collector number) that `CollectionItem`
  points to; `Blueprint` is a printing in the local copy of the CardTrader catalog (`ledger/catalog.py`, loaded lazily
  or nightly by `ledger/cardmarket.py`, which also ties blueprints to Cardmarket prices). `ledger/pricing.py` caches
  CardTrader market prices.
- **Scan pipeline**: photo → `ledger/openrouter.py` (vision model reads the card) → `ledger/recognition.py` (matches
  it to a `Blueprint`) → `ledger/scan.py` (draft lines of a `ScanSession`, saved into the collection at the end).
  The scan API (`ledger/api/scan.py`) is the only one with real DRF serializers (`ledger/api/serializers.py`).
- **API conventions** (`ledger/api/common.py`, `ledger/api/errors.py`): session auth + CSRF (`X-CSRFToken` from the
  `csrftoken` cookie); validation goes through Django forms and `form_errors(form)` turns them into DRF 400s;
  actions answer `{"messages": [{"level", "text"}]}` via `messages_response`/`msg`; other errors are
  `{"detail", "code"}` with custom codes (`Blocked` 409, `ServiceError` 502, ...). Every non-scan view is documented
  with `@doc(tags, summary, request=..., parameters=...)`; request bodies are generated from the validating form with
  `form_body()`, so keep the form and the docs in sync through it. Responses are documented as a generic object.
  Swagger UI is public at `/api/docs/`, schema at `/api/schema/`, version read from `pyproject.toml`.
- **Background work**: Celery with Redis, no beat; tasks (`ledger/tasks.py`) are only queued by management commands
  and scheduled externally.

## Tests

Django's `TestCase` on PostgreSQL. Most ledger tests extend `LedgerTestCase` (`ledger/tests.py`): a superuser `gab`
with demo data and a CardTrader token, a second user `ami`, and `FakeCardTrader` patched into `services.make_client`
(no network). Tests also live in `ledger/test_*.py`, `accounts/tests.py` and `config/tests.py`. `SchemaTest` in
`ledger/test_scan_api.py` fails on any drf-spectacular warning.

## Conventions

- Migrations never run at container start; data migrations exist for the single- to multi-user upgrade.
- `docs/superpowers/` is gitignored (local design notes): never commit it.
- Work on a branch in the main checkout; PRs are squash-merged into `main` and named after the change.
  Releases (GitHub release → Docker image on ghcr.io) bump `version` in `pyproject.toml`.
