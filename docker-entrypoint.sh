#!/bin/sh
set -e

# Only the web app migrates; worker and one-off containers set RUN_MIGRATIONS=false.
if [ "${RUN_MIGRATIONS:-true}" != "false" ]; then
    python manage.py migrate --noinput
fi

exec "$@"
