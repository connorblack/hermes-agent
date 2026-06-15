"""Telegram-specific network helpers.

Provides a hostname-preserving fallback transport for networks where
api.telegram.org resolves to an endpoint that is unreachable from the current
host. The transport keeps the logical request host and TLS SNI as
api.telegram.org while retrying the TCP connection against one or more fallback
IPv4 addresses.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from copy import deepcopy
from typing import Any, Callable, Iterable, Optional

import httpx

logger = logging.getLogger(__name__)

_TELEGRAM_API_HOST = "api.telegram.org"

# DNS-over-HTTPS providers used to discover Telegram API IPs that may differ
# from the (potentially unreachable) IP returned by the local system resolver.
_DOH_TIMEOUT = 4.0  # seconds — bounded so connect() isn't noticeably delayed

_DOH_PROVIDERS: list[dict] = [
    {
        "url": "https://dns.google/resolve",
        "params": {"name": _TELEGRAM_API_HOST, "type": "A"},
        "headers": {},
    },
    {
        "url": "https://cloudflare-dns.com/dns-query",
        "params": {"name": _TELEGRAM_API_HOST, "type": "A"},
        "headers": {"Accept": "application/dns-json"},
    },
]

# Last-resort IPs when DoH is also blocked.  These are stable Telegram Bot API
# endpoints in the 149.154.160.0/20 block (same seed used by OpenClaw).
_SEED_FALLBACK_IPS: list[str] = ["149.154.167.220"]

_WARNING_SUPPRESSION_WINDOW_SECONDS = 60.0


class TelegramNetworkHealth:
    """Compact, synchronously-updated network diagnostic state for Telegram."""

    def __init__(self) -> None:
        ts = _now_ts()
        self._last_dns_resolution: dict[str, Any] = {
            "hostname": _TELEGRAM_API_HOST,
            "resolved_ips": [],
            "resolver_used": "unresolved",
            "success": False,
            "error": None,
            "ts": ts,
        }
        self._last_connect_attempt: dict[str, Any] = {
            "target_ip": None,
            "family": "v4",
            "timeout_seconds": None,
            "success": False,
            "error": None,
            "ts": ts,
        }
        self._current_path = "offline"
        self._last_successful_connection_ts: float | None = None
        self._consecutive_failure_count = 0

    def record_dns_resolution(
        self,
        *,
        hostname: str = _TELEGRAM_API_HOST,
        resolved_ips: Iterable[str] = (),
        resolver_used: str,
        success: bool,
        error: str | None = None,
    ) -> None:
        self._last_dns_resolution = {
            "hostname": hostname,
            "resolved_ips": list(resolved_ips),
            "resolver_used": resolver_used,
            "success": bool(success),
            "error": error,
            "ts": _now_ts(),
        }

    def record_connect_attempt(
        self,
        *,
        target_ip: str,
        family: str,
        timeout_seconds: float | None,
        success: bool,
        error: str | None = None,
        current_path: str | None = None,
    ) -> None:
        self._last_connect_attempt = {
            "target_ip": target_ip,
            "family": family,
            "timeout_seconds": timeout_seconds,
            "success": bool(success),
            "error": error,
            "ts": _now_ts(),
        }
        if success:
            self._last_successful_connection_ts = time.time()
            self._consecutive_failure_count = 0
            self._current_path = current_path or "primary_dns"
        else:
            self._consecutive_failure_count += 1
            if current_path is not None:
                self._current_path = current_path

    def snapshot(self) -> dict[str, Any]:
        if self._last_successful_connection_ts is None:
            age = None
        else:
            age = max(0, int(time.time() - self._last_successful_connection_ts))
        return {
            "last_dns_resolution": deepcopy(self._last_dns_resolution),
            "last_connect_attempt": deepcopy(self._last_connect_attempt),
            "current_path": self._current_path,
            "last_successful_connection_age_seconds": age,
            "consecutive_failure_count": self._consecutive_failure_count,
        }


def _now_ts() -> int:
    return int(time.time())


def _ip_family(value: str) -> str:
    try:
        return "v6" if ipaddress.ip_address(value).version == 6 else "v4"
    except ValueError:
        return "v4"


def _resolve_proxy_url(target_hosts=None) -> str | None:
    # Delegate to shared implementation (env vars + macOS system proxy detection)
    from gateway.platforms.base import resolve_proxy_url
    return resolve_proxy_url("TELEGRAM_PROXY", target_hosts=target_hosts)


class _TelegramNetworkWarningRateLimiter:
    """Collapse repeated network warnings for the same kind/target pair."""

    def __init__(
        self,
        window_seconds: float = _WARNING_SUPPRESSION_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.window_seconds = window_seconds
        self._clock = clock
        self._events: dict[tuple[str, str], tuple[float, int]] = {}

    def check(self, kind: str, target: str) -> tuple[bool, int]:
        """Return (should_emit, suppressed_count_since_last_emit)."""
        key = (kind, target)
        now = self._clock()
        previous = self._events.get(key)
        if previous is None:
            self._events[key] = (now, 0)
            return True, 0

        last_emitted_at, suppressed_count = previous
        if now - last_emitted_at >= self.window_seconds:
            self._events[key] = (now, 0)
            return True, suppressed_count

        self._events[key] = (last_emitted_at, suppressed_count + 1)
        return False, 0

    def clear(self, kind: str, target: str) -> None:
        self._events.pop((kind, target), None)


class TelegramFallbackTransport(httpx.AsyncBaseTransport):
    """Retry Telegram Bot API requests via fallback IPs while preserving TLS/SNI.

    Requests continue to target https://api.telegram.org/... logically, but on
    connect failures the underlying TCP connection is retried against a known
    reachable IP. This is effectively the programmatic equivalent of
    ``curl --resolve api.telegram.org:443:<ip>``.
    """

    def __init__(
        self,
        fallback_ips: Iterable[str],
        *,
        health: TelegramNetworkHealth | None = None,
        connect_timeout_seconds: float | None = 10.0,
        **transport_kwargs,
    ):
        self._fallback_ips = list(dict.fromkeys(_normalize_fallback_ips(fallback_ips)))
        self._health = health or TelegramNetworkHealth()
        self._connect_timeout_seconds = connect_timeout_seconds
        proxy_url = _resolve_proxy_url(target_hosts=[_TELEGRAM_API_HOST, *self._fallback_ips])
        if proxy_url and "proxy" not in transport_kwargs:
            transport_kwargs["proxy"] = proxy_url
        self._primary = httpx.AsyncHTTPTransport(**transport_kwargs)
        self._fallbacks = {
            ip: httpx.AsyncHTTPTransport(**transport_kwargs) for ip in self._fallback_ips
        }
        self._sticky_ip: Optional[str] = None
        self._sticky_lock = asyncio.Lock()
        self._warning_limiter = _TelegramNetworkWarningRateLimiter()
        self._primary_connectivity_degraded = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != _TELEGRAM_API_HOST or not self._fallback_ips:
            return await self._primary.handle_async_request(request)

        sticky_ip = self._sticky_ip
        attempt_order: list[Optional[str]] = [sticky_ip] if sticky_ip else [None]
        if sticky_ip:
            attempt_order.append(None)  # retry primary DNS after sticky failure
        for ip in self._fallback_ips:
            if ip != sticky_ip:
                attempt_order.append(ip)

        last_error: Exception | None = None
        for ip in attempt_order:
            candidate = request if ip is None else _rewrite_request_for_ip(request, ip)
            transport = self._primary if ip is None else self._fallbacks[ip]
            target = ip or _TELEGRAM_API_HOST
            path = "primary_dns" if ip is None else f"fallback_ip:{ip}"
            try:
                response = await transport.handle_async_request(candidate)
                self._health.record_connect_attempt(
                    target_ip=target,
                    family=_ip_family(target),
                    timeout_seconds=self._connect_timeout_seconds,
                    success=True,
                    error=None,
                    current_path=path,
                )
                if ip is None:
                    self._log_primary_recovered_once()
                if ip is not None and self._sticky_ip != ip:
                    async with self._sticky_lock:
                        if self._sticky_ip != ip:
                            self._sticky_ip = ip
                            self._log_rate_limited_warning(
                                "sticky_fallback_promoted",
                                ip,
                                "[Telegram] Primary api.telegram.org path unreachable; using sticky fallback IP %s",
                                ip,
                            )
                return response
            except Exception as exc:
                last_error = exc
                if not _is_retryable_connect_error(exc):
                    raise
                self._health.record_connect_attempt(
                    target_ip=target,
                    family=_ip_family(target),
                    timeout_seconds=self._connect_timeout_seconds,
                    success=False,
                    error=str(exc),
                    current_path="offline",
                )
                if ip is not None and ip == self._sticky_ip:
                    async with self._sticky_lock:
                        if self._sticky_ip == ip:
                            self._sticky_ip = None
                            self._log_rate_limited_warning(
                                "sticky_fallback_failed",
                                ip,
                                "[Telegram] Sticky fallback IP %s failed; resetting to primary DNS path",
                                ip,
                            )
                if ip is None:
                    self._primary_connectivity_degraded = True
                    self._log_rate_limited_warning(
                        "primary_connection_failed",
                        _TELEGRAM_API_HOST,
                        "[Telegram] Primary api.telegram.org connection failed (%s); trying fallback IPs %s",
                        exc,
                        ", ".join(self._fallback_ips),
                    )
                    continue
                self._log_rate_limited_warning(
                    "fallback_ip_failed",
                    ip,
                    "[Telegram] Fallback IP %s failed: %s",
                    ip,
                    exc,
                )
                continue

        if last_error is None:
            raise RuntimeError("All Telegram fallback IPs exhausted but no error was recorded")
        raise last_error

    def get_health_snapshot(self) -> dict[str, Any]:
        return self._health.snapshot()

    async def aclose(self) -> None:
        await self._primary.aclose()
        for transport in self._fallbacks.values():
            await transport.aclose()

    def _log_rate_limited_warning(
        self, kind: str, target: str, message: str, *args: object
    ) -> None:
        should_emit, suppressed_count = self._warning_limiter.check(kind, target)
        if not should_emit:
            return
        if suppressed_count:
            message = (
                f"{message} (suppressed {suppressed_count} similar "
                f"warnings in the last {int(self._warning_limiter.window_seconds)}s)"
            )
        logger.warning(message, *args)

    def _log_primary_recovered_once(self) -> None:
        if not self._primary_connectivity_degraded:
            return
        self._primary_connectivity_degraded = False
        self._warning_limiter.clear("primary_connection_failed", _TELEGRAM_API_HOST)
        logger.info("[Telegram] Primary api.telegram.org connection recovered")


def _normalize_fallback_ips(values: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        raw = str(value).strip()
        if not raw:
            continue
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            logger.warning("Ignoring invalid Telegram fallback IP: %r", raw)
            continue
        if addr.version != 4:
            logger.warning("Ignoring non-IPv4 Telegram fallback IP: %s", raw)
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            logger.warning("Ignoring private/internal Telegram fallback IP: %s", raw)
            continue
        normalized.append(str(addr))
    return normalized


def parse_fallback_ip_env(value: str | None) -> list[str]:
    if not value:
        return []
    parts = [part.strip() for part in value.split(",")]
    return _normalize_fallback_ips(parts)


def _resolve_system_dns() -> set[str]:
    """Return the IPv4 addresses that the OS resolver gives for api.telegram.org."""
    try:
        results = socket.getaddrinfo(_TELEGRAM_API_HOST, 443, socket.AF_INET)
        return {addr[4][0] for addr in results}
    except Exception:
        return set()


async def _query_doh_provider(
    client: httpx.AsyncClient, provider: dict
) -> list[str]:
    """Query one DoH provider and return A-record IPs."""
    try:
        resp = await client.get(
            provider["url"], params=provider["params"], headers=provider["headers"]
        )
        resp.raise_for_status()
        data = resp.json()
        ips: list[str] = []
        for answer in data.get("Answer", []):
            if answer.get("type") != 1:  # A record
                continue
            raw = answer.get("data", "").strip()
            try:
                ipaddress.ip_address(raw)
                ips.append(raw)
            except ValueError:
                continue
        return ips
    except Exception as exc:
        logger.debug("DoH query to %s failed: %s", provider["url"], exc)
        return []


async def discover_fallback_ips(
    health: TelegramNetworkHealth | None = None,
) -> list[str]:
    """Auto-discover Telegram API IPs via DNS-over-HTTPS.

    Resolves api.telegram.org through Google and Cloudflare DoH and returns all
    unique A records.  IPs that match the local system resolver are kept rather
    than excluded: in many networks the system-DNS IP is the most reliable path
    to api.telegram.org and a transient primary-path failure should be retried
    against the same address via the IP-rewrite path before the seed list is
    consulted (#14520).  Falls back to a hardcoded seed list only when DoH
    yields no usable answers.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(_DOH_TIMEOUT)) as client:
        doh_tasks = [_query_doh_provider(client, p) for p in _DOH_PROVIDERS]
        system_dns_task = asyncio.to_thread(_resolve_system_dns)
        results = await asyncio.gather(system_dns_task, *doh_tasks, return_exceptions=True)

    # results[0] = system DNS IPs (set), results[1:] = DoH IP lists
    system_ips: set[str] = results[0] if isinstance(results[0], set) else set()

    doh_ips: list[str] = []
    for r in results[1:]:
        if isinstance(r, list):
            doh_ips.extend(r)

    # Deduplicate preserving order
    seen: set[str] = set()
    candidates: list[str] = []
    for ip in doh_ips:
        if ip not in seen:
            seen.add(ip)
            candidates.append(ip)

    # Validate through existing normalization
    validated = _normalize_fallback_ips(candidates)

    if validated:
        if health is not None:
            health.record_dns_resolution(
                resolved_ips=validated,
                resolver_used="dns-over-https",
                success=True,
                error=None,
            )
        logger.debug("Discovered Telegram fallback IPs via DoH: %s", ", ".join(validated))
        return validated

    if health is not None:
        health.record_dns_resolution(
            resolved_ips=sorted(system_ips),
            resolver_used="dns-over-https",
            success=False,
            error="DoH discovery yielded no usable Telegram A records; using seed fallback IPs",
        )
    logger.info(
        "DoH discovery yielded no usable IPs (system DNS: %s); using seed fallback IPs %s",
        ", ".join(system_ips) or "unknown",
        ", ".join(_SEED_FALLBACK_IPS),
    )
    return list(_SEED_FALLBACK_IPS)


def _rewrite_request_for_ip(request: httpx.Request, ip: str) -> httpx.Request:
    original_host = request.url.host or _TELEGRAM_API_HOST
    url = request.url.copy_with(host=ip)
    headers = request.headers.copy()
    headers["host"] = original_host
    extensions = dict(request.extensions)
    extensions["sni_hostname"] = original_host
    return httpx.Request(
        method=request.method,
        url=url,
        headers=headers,
        stream=request.stream,
        extensions=extensions,
    )


def _is_retryable_connect_error(exc: Exception) -> bool:
    return isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError))
