import json

import pytest

from yandex_calendar_mcp import server
from yandex_calendar_mcp.caldav import CalendarError


def test_refuses_plaintext_or_fallback_keyring(monkeypatch):
    monkeypatch.setattr(server.keyring, "get_keyring", lambda: object())
    with pytest.raises(CalendarError, match="plaintext/fallback"):
        server.system_keyring()


def test_missing_password_fails_without_network(monkeypatch, tmp_path):
    monkeypatch.setenv("YANDEX_CALENDAR_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setenv("YANDEX_CALENDAR_USERNAME", "test@yandex.ru")
    class Fake:
        def get_password(self, *args):
            return None
    monkeypatch.setattr(server, "system_keyring", lambda: Fake())
    with pytest.raises(CalendarError, match="missing from the system keyring"):
        server.load_reader()


def test_local_config_loads_without_writing_secret(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"username": "test@yandex.ru", "timezone": "Europe/Podgorica", "allowed_calendars": ["/calendars/test/events-1/"]}))
    monkeypatch.setenv("YANDEX_CALENDAR_CONFIG", str(path))
    monkeypatch.delenv("YANDEX_CALENDAR_USERNAME", raising=False)
    class Fake:
        def get_password(self, service, username):
            assert service == server.SERVICE and username == "test@yandex.ru"
            return "fixture-secret"
    monkeypatch.setattr(server, "system_keyring", lambda: Fake())
    reader = server.load_reader()
    try:
        assert reader.allowed == {"https://caldav.yandex.ru/calendars/test/events-1/"}
        assert "fixture-secret" not in path.read_text()
    finally:
        reader.close()


def test_password_echo_fallback_is_refused(monkeypatch, tmp_path):
    import warnings
    monkeypatch.setenv("YANDEX_CALENDAR_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(server.sys, "argv", ["server", "credentials", "--username", "test@yandex.ru"])
    def unsafe_prompt(*args, **kwargs):
        warnings.warn("Cannot hide input", server.getpass.GetPassWarning)
        raise AssertionError("Must not read an echoed password")
    monkeypatch.setattr(server.getpass, "getpass", unsafe_prompt)
    with pytest.raises(SystemExit) as result:
        server.main()
    assert result.value.code == 2
