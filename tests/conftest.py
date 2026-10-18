"""Unit tests isolate the shared limiter; live Redis is covered by runtime smoke checks."""

import pytest

from api.core.rate_limit import limiter


@pytest.fixture(autouse=True)
def disable_external_rate_limit(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
