# mtg_ledger
MTG Ledger — Magic: The Gathering collection and CardTrader sales manager (Django).

## Structure

- `config/` — Django project (settings, root URLs)
- `theme/` — look and feel: `base.html` layout with sidebar, `static/theme/app.css` (design tokens + components), shared includes, login/account templates
- `accounts/` — profile page: personal details, password change, CardTrader connection (API token)
- `ledger/` — domain: cards, collection, sale listings, shipments, CardTrader client and sync, CSV import/export

## Getting started

The app uses PostgreSQL. Start a local instance with Docker:

```bash
docker compose up -d db
```

Connection settings come from environment variables (defaults match `compose.yaml`):
`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (all default `mtg_ledger`), `POSTGRES_HOST` (`localhost`),
`POSTGRES_PORT` (`5432`), `POSTGRES_CONN_MAX_AGE` (`60` seconds).

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py seed_demo          # demo data from the design (--reset to wipe)
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Tests: `uv run python manage.py test`

## Docker

The image is published to `ghcr.io/gab-25/mtg_ledger` only when a GitHub release is published (`.github/workflows/release.yml`, after the tests pass). Tests run on every PR and push to `main` (`.github/workflows/tests.yml`).

Run the whole stack (PostgreSQL + app built from the local `Dockerfile`) with Docker Compose:

```bash
docker compose up -d --build
docker compose exec mtg_ledger python manage.py createsuperuser
```

The app is served on http://localhost:8000 (`APP_PORT` to change it). Compose reads overrides from the environment
or a `.env` file — set at least `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD` outside local use.

To run the published image against an existing PostgreSQL:

```bash
docker run -p 8000:8000 \
  -e POSTGRES_HOST=db.example.com -e POSTGRES_PASSWORD=... \
  -e DJANGO_SECRET_KEY=... -e DJANGO_ALLOWED_HOSTS=ledger.example.com \
  -e DJANGO_CSRF_TRUSTED_ORIGINS=https://ledger.example.com \
  ghcr.io/gab-25/mtg_ledger:latest
```

Environment variables: the `POSTGRES_*` ones above, `DJANGO_SECRET_KEY`, `DJANGO_DEBUG` (default `false` in the image),
`DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` (comma-separated), `GUNICORN_WORKERS`, `CARDTRADER_API_TOKEN`.
Migrations run at container start (`docker-entrypoint.sh`) before any command.

## CardTrader

Connect CardTrader from **Profile and settings → Connected services** (the token is checked with CardTrader before it is saved, and never shown again), or export `CARDTRADER_API_TOKEN`.

- **Publish**: listings "To publish" are created/updated on CardTrader with the bulk API. Cards are matched to the
  CardTrader catalog (blueprint) by set code + name the first time; unmatched cards stay pending with a warning.
- **Sync all**: imports sale orders (removing sold copies from the Ledger following CardTrader's CardTrader Zero
  rules), then compares published listings with your CardTrader inventory and opens conflicts.
- **Match market prices**: reads the cheapest offer from other sellers for the same condition/language/foil.
- **Shipments**: orders to ship with packing number, address, printable packing list, tracking code and
  "Mark as shipped".

Client: `ledger/cardtrader.py` (endpoints from https://www.cardtrader.com/docs/api/full/reference).
Condition mapping: M→Mint, NM→Near Mint, EX→Slightly Played, GD→Moderately Played, LP→Played,
PL→Heavily Played, PO→Poor.

## CSV

Columns: `name,set,type,color,condition,language,foil,quantity,price`.
