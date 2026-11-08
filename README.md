# mtg_ledger
MTG Ledger — Magic: The Gathering collection and CardTrader sales manager (Django).

## Structure

- `config/` — Django project (settings, root URLs)
- `theme/` — look and feel: `base.html` layout with sidebar, `static/theme/app.css` (design tokens + components), shared includes, login/account templates
- `accounts/` — profile page: personal details, password change, connected services
- `ledger/` — domain: cards, collection, sale listings, shipments, CardTrader client and sync, CSV import/export

## Getting started

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py seed_demo          # demo data from the design (--reset to wipe)
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Tests: `uv run python manage.py test`

## CardTrader

Set the API token (CardTrader profile → settings) in **Sync → Settings**, or export `CARDTRADER_API_TOKEN`.

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
