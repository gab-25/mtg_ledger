# mtg_ledger_core
MTG Ledger — Magic: The Gathering collection and CardTrader sales manager (Django).

This repository is the server: the data, the CardTrader and OpenRouter integrations and a JSON API under `/api/`.
The interface is a React single-page app in [mtg_ledger_ui](https://github.com/gab-25/mtg_ledger_ui); Django serves
its `index.html` for every page URL and its assets under `/static/ui/`.

## Structure

- `config/` — Django project (settings, root URLs); `config/views.py` serves the built React UI (`index.html` for
  every URL that is not `/api/`, `/admin/` or `/static/`)
- `accounts/` — account API (`accounts/api.py`): who is logged in, login/logout, profile, password, CardTrader token
- `ledger/` — domain: cards, collection, sale listings, shipments, CardTrader client and sync, CSV import/export;
  `ledger/api/` is the JSON API of every page (collection, sale, cards, orders, sync, scan)

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
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Tests: `uv run python manage.py test`

The pages come from the React UI: build it once and point `UI_APP_DIR` at it (default `ui_app/` in the project,
gitignored). Without an `index.html` there every page answers *UI not built*.

```bash
(cd ../mtg_ledger_ui && npm install && npm run build)
UI_APP_DIR=../mtg_ledger_ui/dist uv run python manage.py runserver
```

To work on the interface, run Django as above (no `UI_APP_DIR` needed) and `npm run dev` in mtg_ledger_ui: Vite
serves the whole site with live reload and forwards `/api/`, `/admin/` and `/static/` to Django.

## Docker

The image is published to `ghcr.io/gab-25/mtg_ledger_core` only when a GitHub release is published (`.github/workflows/release.yml`, after the tests pass). Tests run on every PR and push to `main` (`.github/workflows/tests.yml`).

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
  ghcr.io/gab-25/mtg_ledger_core:latest
```

Environment variables: the `POSTGRES_*` ones above, `DJANGO_SECRET_KEY`, `DJANGO_DEBUG` (default `false` in the image),
`DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` (comma-separated), `DJANGO_BEHIND_HTTPS_PROXY` (`true` when a
reverse proxy terminates HTTPS and sets `X-Forwarded-Proto`), `GUNICORN_WORKERS`,
`OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `CELERY_BROKER_URL` (default `redis://localhost:6379/0`).
The app container applies pending migrations (`python manage.py migrate`) every time it starts, before gunicorn; the
Celery worker does not. If a migration fails the container exits without serving, and its logs show why.

## Users

Several people can share one Ledger, each with their own collection: every user sees and changes only their own
cards, listings, orders, scan sessions, sync conflicts and log entries (another user's object answers 404). The card
catalog and the prices are shared. The administrator creates the accounts in the Django admin (`/admin/`); there is no
sign-up page.

Upgrading a single-user Ledger: the existing data goes to the first superuser, so create one before upgrading if there
is none (the migration stops with a message otherwise, and the new container does not start: create the superuser with
the old image, or with a one-off container of the new one, e.g.
`docker compose run --rm mtg_ledger python manage.py createsuperuser`, which skips the migrations).

## CardTrader

Every user connects their own CardTrader token from **Profile and settings → Connected services** (the token is checked
with CardTrader before it is saved, and never shown again); it is used only for that user's listings, orders and
shipments, and a CardTrader account can be connected to one user only. The administrator's token (the first superuser
with one) also loads the shared card catalog and prices and recognises everyone's scanned cards, so it must be connected
for scanning to work. `CARDTRADER_API_TOKEN` is no longer read (nor passed by `compose.yaml`): on upgrade, if it is
still set where `migrate` runs and no token was saved in the profile, the migration copies it into the first
superuser's profile; otherwise connect the token from the profile after upgrading.

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

## Card database and Cardmarket prices

Every night the Ledger reloads its card database from CardTrader (every MTG expansion and every printing, i.e.
blueprint) and Cardmarket's public price guide (low, trend, average and the 1/7/30-day averages, regular and foil).
Each CardTrader blueprint carries its Cardmarket product id, which ties it to its price. Your cards are matched to a
printing by their CardTrader blueprint, or else by set code + collector number; the sync logs how many cards have no
printing. It uses the administrator's CardTrader token (Profile and settings).

- **Cards** page: search the card database by name, set code or collector number (or only the cards you own) and see
  how many copies you own and the Cardmarket prices.
- **Listing form**: the Cardmarket trend (foil trend for foil copies) is suggested next to the CardTrader market
  price; "Use" copies it into the price field.

The sync runs on a Celery worker (Redis broker). `sync_cardmarket` only queues it; schedule it every night after
03:00 (Cardmarket regenerates the price guide around 02:45 CET) with the scheduler of your choice.

```bash
docker compose up -d redis
uv run celery -A config worker --loglevel=info      # the worker
uv run python manage.py sync_cardmarket             # queue a sync (outcome in Sync → Activity log)
```

With Docker Compose the `worker` and `redis` services start with the stack; queue a sync with
`docker compose exec mtg_ledger python manage.py sync_cardmarket`.

## Scanning cards with the camera

**Collection → Scan cards** opens a phone-friendly flow: pick an expansion (or *All expansions*) and a default
condition, then frame one card at a time. **Auto-shoot** takes the photo by itself as soon as a card is in the box
and in focus, and re-arms only once the box is empty again (the shutter button still works). The photo is
recognised by a vision model on OpenRouter, matched against the local CardTrader catalog and added to a draft.
Uncertain cards ask you to pick among candidates; **Review** shows the draft (duplicates merged), lets you set a
"list for sale above €X" threshold and saves everything at once (new copies get the CardTrader market price; listings
are created as *To publish*).

- The scan screens are part of the React UI ([mtg_ledger_ui](https://github.com/gab-25/mtg_ledger_ui)), under
  `/scan/...`; they talk to the JSON API under `/api/scan/`. All the scan logic stays here.
- The API is built with Django REST Framework (`ledger/api/`, `accounts/api.py`), with the Django session and the
  `csrftoken` cookie (sent back as `X-CSRFToken`). Its OpenAPI schema is at `/api/schema/` and the interactive docs
  at `/api/docs/` (both public); `uv run python manage.py spectacular --file schema.yaml` writes the schema.
  Errors use DRF's format: `{"field": ["message"]}` for validation (400), otherwise `{"detail", "code",
  "help_url"?}`, with `code` such as `not_authenticated` (403), `not_found`, `closed` (409: the session was already
  saved), `blocked` (409), `service_missing` (503) or `service_error` (502: CardTrader failed). Actions answer
  `{"messages": [{"level", "text"}]}`, shown by the UI like Django's messages.
- The Docker image downloads a pinned release of the UI at build time (`UI_VERSION` in the `Dockerfile`, currently
  `v0.3.1`): to ship a new version of the interface, publish a release in mtg_ledger_ui, bump `UI_VERSION` and
  rebuild. `docker build --build-arg UI_VERSION=vX.Y.Z .` tries another release without editing the file.
- OpenRouter is configured only on the server, through `OPENROUTER_API_KEY` (it cannot be set from the profile);
  `OPENROUTER_MODEL` picks the vision model (e.g. `qwen/qwen3.8-flash`) and has no default: card recognition
  needs both.
- The catalog loads lazily from CardTrader; `uv run python manage.py sync_catalog [--set CODE | --all]` refreshes it.
- The camera works on **smartphones only** (webcams can't focus on a card up close). A session started on a
  computer shows a **QR code** that opens the same session on the phone; Review, confirmation and search work on
  both.
- The browser only gives camera access over **HTTPS**.

### Trying it from your phone in development (Tailscale)

The camera needs HTTPS. [Tailscale](https://tailscale.com) gives the development server an HTTPS address with a
real certificate that only your own devices can reach; mtg_ledger_ui's `npm run dev:phone` publishes it.

1. Install Tailscale on the computer and on the phone, signed in with the same account, and keep it connected on
   the phone while testing. In the Tailscale admin console enable **MagicDNS** and **HTTPS Certificates**. If
   `tailscale serve` is refused, allow your user once with `sudo tailscale set --operator=$USER`.
2. Allow the tailnet name of this computer (`<pc>.<tailnet>.ts.net`, shown by `tailscale status`) in `.env`:
   ```
   DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,<pc>.<tailnet>.ts.net
   DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000,https://<pc>.<tailnet>.ts.net
   DJANGO_BEHIND_HTTPS_PROXY=true
   ```
   With `DJANGO_BEHIND_HTTPS_PROXY` Django trusts `X-Forwarded-Proto` from Tailscale; plain `localhost` requests
   carry no such header and stay HTTP.
3. Start the database (`docker compose up -d db`) and Django (`uv run python manage.py runserver`), with
   `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` set for card recognition.
4. In mtg_ledger_ui run `npm run dev:phone`: it checks Tailscale and that Django accepts the tailnet name, then
   publishes the Vite dev server (the UI with live reload, `/api/`, `/admin/` and `/static/` forwarded to Django) with
   `tailscale serve` and prints the `https://<pc>.<tailnet>.ts.net` address. Ctrl+C removes the Tailscale rule.
5. Open that address on the computer, connect CardTrader in **Profile and settings**, start a session in
   **Scan cards** and scan the QR code with the phone.

To try the production setup instead (Django serving the built UI, as in the Docker image), build it with
`npm run build` in mtg_ledger_ui, start Django with `UI_APP_DIR` pointing at its `dist/` and expose port 8000
with `tailscale serve --https=443 8000`.

If the QR page says the address is not HTTPS, the proxy did not send `X-Forwarded-Proto`.

## CSV

Columns: `name,set,type,color,condition,language,foil,quantity,price,number` (`number` = collector number, optional: it identifies the exact printing).
