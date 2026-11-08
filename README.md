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
`DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` (comma-separated), `DJANGO_BEHIND_HTTPS_PROXY` (`true` when a
reverse proxy terminates HTTPS and sets `X-Forwarded-Proto`), `GUNICORN_WORKERS`, `CARDTRADER_API_TOKEN`,
`OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `CELERY_BROKER_URL` (default `redis://localhost:6379/0`), `RUN_MIGRATIONS`.
Migrations run at container start (`docker-entrypoint.sh`) before any command, unless `RUN_MIGRATIONS=false`
(set it on the worker and on one-off containers, so only the web app migrates).

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

## Cardmarket prices

The Ledger keeps a local copy of every Cardmarket MTG single with its price guide (low, trend, average and the
1/7/30-day averages, regular and foil), from Cardmarket's public daily files. Scryfall's `default_cards` bulk data
ties each Cardmarket product to its set + collector number, which is how your cards are linked to it (cards whose
set code differs from Scryfall's, or without a collector number, stay unlinked; the sync logs how many).

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
condition, then frame one card at a time and tap the shutter. The photo is recognised by a vision model on
OpenRouter, matched against the local CardTrader catalog and added to a draft.
Uncertain cards ask you to pick among candidates; **Review** shows the draft (duplicates merged), lets you set a
"list for sale above €X" threshold and saves everything at once (new copies get the CardTrader market price; listings
are created as *To publish*).

- Connect OpenRouter from **Profile and settings → Connected services** (or export `OPENROUTER_API_KEY`);
  `OPENROUTER_MODEL` picks the vision model (default `google/gemini-2.5-flash`).
- The catalog loads lazily from CardTrader; `uv run python manage.py sync_catalog [--set CODE | --all]` refreshes it.
- The camera page works on **smartphones only** (webcams can't focus on a card up close). A session started on a
  computer shows a **QR code** that opens the same session on the phone; Review, confirmation and search work on
  both.
- The browser only gives camera access over **HTTPS**.

### Trying it from your phone in development (Tailscale)

[Tailscale](https://tailscale.com) gives the development server an HTTPS address with a real certificate that only
your own devices can reach.

1. Install Tailscale on the computer and on the phone, signed in with the same account, and keep it connected on
   the phone while testing.
2. In the Tailscale admin console enable **MagicDNS** and **HTTPS Certificates**.

Then start the database (`docker compose up -d db`) and run `scripts/dev-phone.sh` (option: `PORT`, default 8000).
It checks Tailscale and the database connection, sets the Django host/CSRF/proxy variables, publishes the server
with `tailscale serve` and removes that rule on Ctrl+C. It never runs migrations: if some are missing it stops
and asks you to run `uv run python manage.py migrate`. If `tailscale serve` is refused, allow your user once with
`sudo tailscale set --operator=$USER`.

The same steps by hand:

3. Expose the development server on the tailnet:
   ```bash
   tailscale serve --https=443 8000
   ```
   It prints the address, e.g. `https://my-pc.my-tailnet.ts.net`.
4. Start Django on that port, trusting the Tailscale proxy:
   ```bash
   export DJANGO_ALLOWED_HOSTS=.ts.net,localhost
   export DJANGO_CSRF_TRUSTED_ORIGINS=https://*.ts.net
   export DJANGO_BEHIND_HTTPS_PROXY=true
   uv run python manage.py runserver 8000
   ```
5. Open the `ts.net` address on the computer, connect OpenRouter and CardTrader in **Profile and settings**, start a
   session in **Scan cards** and scan the QR code with the phone.

If the QR page still says the address is not HTTPS, the proxy did not send `X-Forwarded-Proto`.

## CSV

Columns: `name,set,type,color,condition,language,foil,quantity,price,number` (`number` = collector number, optional: it identifies the exact printing).
