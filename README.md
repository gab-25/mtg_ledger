# mtg_ledger
MTG Ledger — Magic: The Gathering collection and CardTrader sales manager (Django).

## Structure

- `config/` — Django project (settings, root URLs)
- `theme/` — look and feel: `base.html` layout with sidebar, `static/theme/app.css` (design tokens + components), shared includes, login/account templates
- `ledger/` — domain: cards, collection, sale listings, sync with CardTrader, CSV import/export

## Getting started

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py seed_demo          # demo data from the design (--reset to wipe)
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Tests: `uv run python manage.py test`

## Notes

The CardTrader integration is not wired to the real API yet: "Publish", "Sync all" and conflict
resolution update local state and the activity log (`ledger/services.py`).
CSV columns: `name,set,type,color,condition,language,foil,quantity,price`.
