import os

from zoomollo import settings


def test_settings_file_loads_keys(tmp_path, monkeypatch):
    (tmp_path / settings.FILENAME).write_text("# comment\nBRAVE_API_KEY= abc123 \nSERPAPI_KEY=\nZEROBOUNCE_API_KEY=\"zz\"\n")
    monkeypatch.chdir(tmp_path)
    for k in ("BRAVE_API_KEY", "SERPAPI_KEY", "ZEROBOUNCE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert settings.load_settings() == tmp_path / settings.FILENAME
    assert os.environ["BRAVE_API_KEY"] == "abc123" and os.environ["ZEROBOUNCE_API_KEY"] == "zz"
    assert "SERPAPI_KEY" not in os.environ   # blank values are ignored
    monkeypatch.setenv("BRAVE_API_KEY", "fromenv")
    settings.load_settings()
    assert os.environ["BRAVE_API_KEY"] == "fromenv"   # real environment wins
