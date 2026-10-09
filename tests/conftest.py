"""Tests never inherit operator credentials or local dotenv configuration."""

import os

import pytest

from vibesearch.config import Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for name in list(os.environ):
        if name.startswith("VIBESEARCH_"):
            monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
