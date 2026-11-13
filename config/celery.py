"""Celery app. Tasks are queued by management commands (e.g. sync_catalog); no beat schedule is configured."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("mtg_ledger")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
