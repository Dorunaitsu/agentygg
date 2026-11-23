"""No test ever opens a real browser (the device login does on a person's machine)."""

import pytest


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    monkeypatch.setenv("YGG_NO_BROWSER", "1")
