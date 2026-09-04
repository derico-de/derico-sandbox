import pytest


@pytest.fixture(autouse=True)
def hermetic_state_home(tmp_path, monkeypatch):
    """Keep tests away from the developer's real approvals and endpoints."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
