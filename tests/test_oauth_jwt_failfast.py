"""Startup fails fast when replicas would sign JWTs with different ephemeral keys."""

import logging

import pytest

from deapi_mcp.config import settings
from deapi_mcp.server_remote import require_jwt_secret_key


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    monkeypatch.delenv("DEAPI_JWT_SECRET_KEY", raising=False)
    monkeypatch.setattr(settings, "redis_host", None)


@pytest.mark.parametrize("host", ["0.0.0.0", "10.1.2.3"])
def test_exits_when_bound_beyond_loopback_without_key(host, caplog):
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
        require_jwt_secret_key(host)
    assert exc.value.code == 1
    assert "DEAPI_JWT_SECRET_KEY" in caplog.text


def test_exits_when_redis_configured_even_on_localhost(monkeypatch):
    monkeypatch.setattr(settings, "redis_host", "redis.internal")
    with pytest.raises(SystemExit) as exc:
        require_jwt_secret_key("127.0.0.1")
    assert exc.value.code == 1


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_localhost_without_redis_stays_lenient(host):
    require_jwt_secret_key(host)


def test_key_set_never_exits(monkeypatch):
    monkeypatch.setenv("DEAPI_JWT_SECRET_KEY", "x" * 40)
    monkeypatch.setattr(settings, "redis_host", "redis.internal")
    require_jwt_secret_key("0.0.0.0")


def test_empty_key_counts_as_unset(monkeypatch):
    monkeypatch.setenv("DEAPI_JWT_SECRET_KEY", "")
    with pytest.raises(SystemExit):
        require_jwt_secret_key("0.0.0.0")
