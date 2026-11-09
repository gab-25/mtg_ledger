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
docker compose exec mtg_ledger python manage.py migrate
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
`DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` (comma-separated), `DJANGO_BEHIND_HTTPS_PROXY` (`true` when a
reverse proxy terminates HTTPS and sets `X-Forwarded-Proto`), `GUNICORN_WORKERS`, `CARDTRADER_API_TOKEN`,
`OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `CELERY_BROKER_URL` (default `redis://localhost:6379/0`).
Migrations never run on their own at container start: run `python manage.py migrate` yourself after each upgrade
(e.g. `docker compose exec mtg_ledger python manage.py migrate`, or a one-off container of the image with that command).

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

## Card database and Cardmarket prices

Every night the Ledger reloads its card database from CardTrader (every MTG expansion and every printing, i.e.
blueprint) and Cardmarket's public price guide (low, trend, average and the 1/7/30-day averages, regular and foil).
Each CardTrader blueprint carries its Cardmarket product id, which ties it to its price. Your cards are matched to a
printing by their CardTrader blueprint, or else by set code + collector number; the sync logs how many cards have no
printing. It needs the CardTrader API token (environment or Profile and settings) on the worker too.

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

- The scan screens are a React single-page app, [mtg_ledger_scan](https://github.com/gab-25/mtg_ledger_scan).
  MTG Ledger serves its `index.html` for every `/scan/...` URL (login required) and its assets under
  `/static/scan/`; the app talks to the JSON API under `/api/scan/` with the Django session and the `csrftoken`
  cookie (sent back as `X-CSRFToken`). All the scan logic stays here.
- The API is built with Django REST Framework (`ledger/api/`). Its OpenAPI schema is at `/api/schema/` and the
  interactive docs at `/api/docs/` (both need login); `uv run python manage.py spectacular --file schema.yaml`
  writes the schema, e.g. to generate the TypeScript types of the scan app. Errors use DRF's format:
  `{"field": ["message"]}` for validation (400), otherwise `{"detail", "code", "help_url"?}`, with `code` such as
  `not_authenticated` (403), `not_found`, `closed` (409: the session was already saved) or `service_missing` (503).
- `SCAN_APP_DIR` is the folder with the built app (default `scan_app/` in the project, gitignored). Without an
  `index.html` there, `/scan/` answers *Scan app not built*. In development build the app and point at it:
  ```bash
  (cd ../mtg_ledger_scan && npm run build)
  SCAN_APP_DIR=../mtg_ledger_scan/dist uv run python manage.py runserver
  ```
- The Docker image downloads the latest release of the app at build time: to ship a new version of the scan app,
  publish a release in mtg_ledger_scan and rebuild.
- OpenRouter is configured only on the server, through `OPENROUTER_API_KEY` (it cannot be set from the profile);
  `OPENROUTER_MODEL` picks the vision model (default `qwen/qwen3.8-flash`).
- The catalog loads lazily from CardTrader; `uv run python manage.py sync_catalog [--set CODE | --all]` refreshes it.
- The camera works on **smartphones only** (webcams can't focus on a card up close). A session started on a
  computer shows a **QR code** that opens the same session on the phone; Review, confirmation and search work on
  both.
- The browser only gives camera access over **HTTPS**.

### Trying it from your phone in development (Tailscale)

The camera needs HTTPS. [Tailscale](https://tailscale.com) gives the development server an HTTPS address with a
real certificate that only your own devices can reach; mtg_ledger_scan's `npm run dev:phone` publishes it.

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
   `OPENROUTER_API_KEY` set for card recognition.
4. In mtg_ledger_scan run `npm run dev:phone`: it checks Tailscale and that Django accepts the tailnet name, then
   publishes the Vite dev server (the scan app with live reload, every other URL forwarded to Django) with
   `tailscale serve` and prints the `https://<pc>.<tailnet>.ts.net` address. Ctrl+C removes the Tailscale rule.
5. Open that address on the computer, connect CardTrader in **Profile and settings**, start a session in
   **Scan cards** and scan the QR code with the phone.

To try the production setup instead (Django serving the built app, as in the Docker image), build it with
`npm run build` in mtg_ledger_scan, start Django with `SCAN_APP_DIR` pointing at its `dist/` and expose port 8000
with `tailscale serve --https=443 8000`.

If the QR page says the address is not HTTPS, the proxy did not send `X-Forwarded-Proto`.

## CSV

Columns: `name,set,type,color,condition,language,foil,quantity,price,number` (`number` = collector number, optional: it identifies the exact printing).
