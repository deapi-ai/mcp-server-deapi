"""Tests for the Redis settings and their startup validation."""

import pytest
from pydantic import ValidationError

from deapi_mcp.config import Settings


@pytest.fixture(autouse=True)
def _clean_redis_env(monkeypatch):
    for name in (
        "HOST",
        "PORT",
        "USERNAME",
        "PASSWORD",
        "DB",
        "SCHEME",
        "CA_FILE",
        "PREFIX",
        "REQUIRED",
    ):
        monkeypatch.delenv(f"DEAPI_REDIS_{name}", raising=False)


def test_redis_defaults():
    s = Settings(_env_file=None)
    assert (s.redis_host, s.redis_port, s.redis_db) == (None, 6379, 11)
    assert (s.redis_scheme, s.redis_prefix, s.redis_required) == (
        "plain",
        "deapi_mcp_database_",
        False,
    )


def test_redis_env_names(monkeypatch):
    monkeypatch.setenv("DEAPI_REDIS_HOST", "redis.example.com")
    monkeypatch.setenv("DEAPI_REDIS_SCHEME", "tls")
    monkeypatch.setenv("DEAPI_REDIS_CA_FILE", "/etc/redis-ca/ca.pem")
    monkeypatch.setenv("DEAPI_REDIS_USERNAME", "mcp-app")
    s = Settings(_env_file=None)
    assert (s.redis_host, s.redis_scheme, s.redis_username) == (
        "redis.example.com",
        "tls",
        "mcp-app",
    )


def test_tls_without_ca_file_rejected(monkeypatch):
    monkeypatch.setenv("DEAPI_REDIS_HOST", "h")
    monkeypatch.setenv("DEAPI_REDIS_SCHEME", "tls")
    with pytest.raises(ValidationError, match="DEAPI_REDIS_CA_FILE"):
        Settings(_env_file=None)


def test_required_without_host_rejected(monkeypatch):
    monkeypatch.setenv("DEAPI_REDIS_REQUIRED", "true")
    with pytest.raises(ValidationError, match="DEAPI_REDIS_HOST"):
        Settings(_env_file=None)


def test_password_never_appears_in_errors_or_repr(monkeypatch):
    monkeypatch.setenv("DEAPI_REDIS_PASSWORD", "s3cret-value")
    monkeypatch.setenv("DEAPI_REDIS_HOST", "h")
    monkeypatch.setenv("DEAPI_REDIS_SCHEME", "tls")
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert "s3cret-value" not in str(exc.value)

    monkeypatch.delenv("DEAPI_REDIS_SCHEME")
    s = Settings(_env_file=None)
    assert s.redis_password.get_secret_value() == "s3cret-value"
    assert "s3cret-value" not in repr(s)
    assert "s3cret-value" not in str(s)


def test_metrics_token_stripped_and_min_length():
    s = Settings(_env_file=None, metrics_token="  " + "x" * 32 + "\n")
    assert s.metrics_token.get_secret_value() == "x" * 32
    with pytest.raises(ValidationError):
        Settings(_env_file=None, metrics_token="short")
    assert Settings(_env_file=None, metrics_token="").metrics_token is None


def test_short_metrics_token_never_appears_in_errors(monkeypatch):
    short = "short-metrics-token-value"
    monkeypatch.setenv("DEAPI_METRICS_TOKEN", short)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert "DEAPI_METRICS_TOKEN must be at least 32 characters" in str(exc.value)
    assert short not in str(exc.value)
    assert short not in repr(exc.value)


def test_metrics_db_and_prefix_defaults(monkeypatch):
    for name in ("TOKEN", "REDIS_DB", "PREFIX"):
        monkeypatch.delenv(f"DEAPI_METRICS_{name}", raising=False)
    s = Settings(_env_file=None)
    assert (s.metrics_redis_db, s.metrics_prefix, s.metrics_token) == (
        12,
        "deapi_mcp_metrics_",
        None,
    )


def test_build_redis_client_db_override():
    from deapi_mcp.auth_code_store import build_redis_client

    s = Settings(_env_file=None, redis_host="redis.example.com")
    kwargs = build_redis_client(s, db=12, timeout=0.5, retries=0).connection_pool.connection_kwargs
    assert kwargs["db"] == 12
    assert kwargs["socket_timeout"] == 0.5
    default = build_redis_client(s).connection_pool.connection_kwargs
    assert (default["db"], default["socket_timeout"]) == (11, 2)
