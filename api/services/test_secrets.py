import pytest

from api.services import secrets


async def test_get_secret_reads_from_env_in_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("MY_SECRET", "value-123")

    assert await secrets.get_secret("MY_SECRET") == "value-123"


async def test_get_secret_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.delenv("MISSING_SECRET", raising=False)

    with pytest.raises(KeyError):
        await secrets.get_secret("MISSING_SECRET")
