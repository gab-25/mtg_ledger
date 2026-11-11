# The React UI (gab-25/mtg_ledger_ui), pinned to a release: bump UI_VERSION to ship a new interface
# (or override it with --build-arg). Extracted in its own stage so the tarball never lands in the image.
FROM alpine:3.22 AS ui
ARG UI_VERSION=v0.3.1
ADD https://github.com/gab-25/mtg_ledger_ui/releases/download/${UI_VERSION}/mtg-ledger-ui.tar.gz /ui.tgz
RUN mkdir /ui && tar -xzf /ui.tgz -C /ui

FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_DEBUG=false

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .

COPY --from=ui /ui /app/ui_app

RUN python manage.py collectstatic --noinput

RUN useradd --system --create-home --uid 1000 app
USER app
EXPOSE 8000

# Apply pending migrations before serving (the Celery worker overrides CMD and skips this).
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-2}"]
