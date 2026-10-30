"""Single-use OAuth authorization code storage.

Codes issued by /authorize must be redeemable on any replica, so the production
backend is Redis. The in-memory backend keeps single-replica deployments and local
development working without it.

The Redis instance is shared with other applications and runs with noeviction, so
every key carries a TTL, lives under the configured prefix, and the global cap is
enforced atomically by a Lua script. Raw codes never appear in key names.
"""

import hashlib
import json
import logging
import time
from typing import Any, Protocol, cast

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from .config import Settings, settings

logger = logging.getLogger(__name__)

_PUT_SCRIPT = """
if tonumber(ARGV[4]) <= 0 then return 0 end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[3])
while redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[4]) do
  local oldest = redis.call('ZPOPMIN', KEYS[2])
  redis.call('DEL', ARGV[6] .. oldest[1])
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('ZADD', KEYS[2], tonumber(ARGV[3]) + tonumber(ARGV[2]), ARGV[5])
redis.call('EXPIRE', KEYS[2], ARGV[2])
return 1
"""


class AuthCodeStoreUnavailable(Exception):  # noqa: N818
    """The backing store could not be reached or rejected the operation."""


class AuthCodeStore(Protocol):
    async def put(self, code: str, data: dict[str, Any], ttl_seconds: int) -> bool:
        """Store a code, evicting the oldest live code when the store is full.

        The cap bounds memory; eviction keeps a burst of unauthenticated /authorize calls
        from locking real users out. The per-IP ingress rate limit is tracked separately
        for DevOps. Returns False only if the code could not be stored.
        """
        ...

    async def consume(self, code: str) -> dict[str, Any] | None:
        """Atomically fetch and delete a code. None if unknown, used or expired."""
        ...


class InMemoryAuthCodeStore:
    def __init__(self, max_entries: int) -> None:
        self._max_entries = max_entries
        self._codes: dict[str, tuple[dict[str, Any], float]] = {}

    def _prune(self, now: float) -> None:
        for key in [k for k, (_, exp) in self._codes.items() if exp <= now]:
            del self._codes[key]

    async def put(self, code: str, data: dict[str, Any], ttl_seconds: int) -> bool:
        now = time.time()
        self._prune(now)
        if self._max_entries <= 0:
            return False
        while len(self._codes) >= self._max_entries:
            del self._codes[next(iter(self._codes))]  # dicts keep insertion order: oldest first
        self._codes[code] = (data, now + ttl_seconds)
        return True

    async def consume(self, code: str) -> dict[str, Any] | None:
        entry = self._codes.pop(code, None)
        if entry is None:
            return None
        data, expires = entry
        if expires <= time.time():
            return None
        return data


class RedisAuthCodeStore:
    def __init__(self, client: aioredis.Redis, prefix: str, max_entries: int) -> None:
        self._client = client
        self._prefix = prefix
        self._max_entries = max_entries
        self._index_key = f"{prefix}oauth_codes_live"
        self._put_script = client.register_script(_PUT_SCRIPT)

    def _code_key(self, digest: str) -> str:
        return f"{self._prefix}oauth_code:{digest}"

    @staticmethod
    def _digest(code: str) -> str:
        return hashlib.sha256(code.encode()).hexdigest()

    async def put(self, code: str, data: dict[str, Any], ttl_seconds: int) -> bool:
        digest = self._digest(code)
        try:
            result = await self._put_script(
                keys=[self._code_key(digest), self._index_key],
                args=[
                    json.dumps(data),
                    ttl_seconds,
                    time.time(),
                    self._max_entries,
                    digest,
                    self._code_key(""),
                ],
            )
        except RedisError as exc:
            raise AuthCodeStoreUnavailable("Redis auth code store unavailable") from exc
        return int(result) == 1

    async def consume(self, code: str) -> dict[str, Any] | None:
        digest = self._digest(code)
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.getdel(self._code_key(digest))
                pipe.zrem(self._index_key, digest)
                raw, _ = await pipe.execute()
        except RedisError as exc:
            raise AuthCodeStoreUnavailable("Redis auth code store unavailable") from exc
        if raw is None:
            return None
        return cast(dict[str, Any], json.loads(raw))


def build_redis_client(
    s: Settings,
    *,
    db: int | None = None,
    timeout: float = 2.0,
    retries: int = 1,
    max_connections: int | None = None,
) -> aioredis.Redis:
    kwargs: dict[str, Any] = {
        "host": s.redis_host,
        "port": s.redis_port,
        "db": s.redis_db if db is None else db,
        "username": s.redis_username,
        "password": s.redis_password.get_secret_value() if s.redis_password else None,
        "socket_connect_timeout": timeout,
        "socket_timeout": timeout,
        # redis-py >= 6 defaults to 3 retries with exponential backoff, which makes a login
        # against an unreachable Redis hang 9-19 s. One immediate retry still survives a
        # stale pooled connection after a managed-Redis restart.
        "retry": Retry(NoBackoff(), retries),
        "decode_responses": True,
    }
    if max_connections is not None:
        kwargs["max_connections"] = max_connections
    if s.redis_scheme == "tls":
        kwargs.update(
            ssl=True,
            ssl_ca_certs=s.redis_ca_file,
            ssl_cert_reqs="required",
            ssl_check_hostname=True,
        )
    return aioredis.Redis(**kwargs)


_store: AuthCodeStore | None = None

AUTHORIZATION_CODE_MAX_ENTRIES = 1000


def get_auth_code_store() -> AuthCodeStore:
    global _store
    if _store is None:
        if settings.redis_host:
            logger.info(
                "OAuth code store: Redis %s:%s db=%s prefix=%s",
                settings.redis_host,
                settings.redis_port,
                settings.redis_db,
                settings.redis_prefix,
            )
            _store = RedisAuthCodeStore(
                build_redis_client(settings), settings.redis_prefix, AUTHORIZATION_CODE_MAX_ENTRIES
            )
        else:
            logger.warning(
                "OAuth code store: in-memory (DEAPI_REDIS_HOST unset). Authorization codes are "
                "per-process, so this is only safe for a single-replica deployment"
            )
            _store = InMemoryAuthCodeStore(AUTHORIZATION_CODE_MAX_ENTRIES)
    return _store


def reset_auth_code_store(store: AuthCodeStore | None = None) -> None:
    global _store
    _store = store
