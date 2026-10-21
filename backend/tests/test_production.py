import pytest

from config.production import validate_startup_config


def test_production_requires_hmac(monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "production")
    monkeypatch.setenv("AGENT_AUTH_MODE", "off")
    monkeypatch.delenv("AGENT_INTERNAL_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        validate_startup_config()


def test_hmac_mode_requires_secret(monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "dev")
    monkeypatch.setenv("AGENT_AUTH_MODE", "hmac")
    monkeypatch.delenv("AGENT_INTERNAL_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        validate_startup_config()


def test_dev_default_is_allowed(monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "dev")
    monkeypatch.setenv("AGENT_AUTH_MODE", "off")
    validate_startup_config()  # 不应抛异常
