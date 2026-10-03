import pytest


@pytest.fixture(autouse=True)
def isolate_fetch_instance_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("ARCHIVE_MAGIC_FETCH_CONFIG", raising=False)
