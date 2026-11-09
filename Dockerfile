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

# The React scan app (gab-25/mtg_ledger_scan), pinned: bump SCAN_VERSION to ship a new release.
ARG SCAN_VERSION=0.1.0
ADD https://github.com/gab-25/mtg_ledger_scan/releases/download/v${SCAN_VERSION}/mtg-ledger-scan-${SCAN_VERSION}.tar.gz /tmp/scan.tgz
RUN rm -rf /app/scan_app && mkdir -p /app/scan_app && tar -xzf /tmp/scan.tgz -C /app/scan_app && rm /tmp/scan.tgz

RUN python manage.py collectstatic --noinput

RUN useradd --system --create-home --uid 1000 app
USER app
EXPOSE 8000

CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-2}"]
