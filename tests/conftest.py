import os
import sys

import pytest

# Make `tests/fakes.py` importable as `fakes`.
sys.path.insert(0, os.path.dirname(__file__))

# Modules that read settings at import time (the Celery app) must work without
# a real .env, e.g. when the self-coding worker runs the suite in a clean clone.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ.setdefault("ALLOWED_TELEGRAM_USER_ID", "1")

from fakes import InMemoryEventStore, make_services  # noqa: E402

from uva.logging import events  # noqa: E402


@pytest.fixture
def event_store():
    store = InMemoryEventStore()
    events.set_store(store)
    yield store
    events.set_store(None)


@pytest.fixture
def services(event_store):
    return make_services(events=event_store)
