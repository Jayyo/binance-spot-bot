import os

import pytest


@pytest.fixture(autouse=True)
def isolated_from_real_keys(monkeypatch, tmp_path):
    """No test may see the developer's real Binance variables or ./.env."""
    for name in list(os.environ):
        if name.startswith("BINANCE"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
