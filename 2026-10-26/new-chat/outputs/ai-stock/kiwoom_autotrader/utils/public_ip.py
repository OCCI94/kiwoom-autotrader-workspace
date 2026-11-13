"""Lazy, bounded public egress IP lookup with no broker credentials.

IP-only error codes come from /tr_docs/오류코드.md, source rows 28 and 37:
8010 is token/request IP mismatch; 8050 is an unregistered IP. Authentication,
HTTP status and generic transport failures are not evidence of an IP mismatch.
Providers: https://www.ipify.org/ and Amazon's checkip.amazonaws.com endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import threading
import time
import urllib.request

from .http_queue import queued_open


_PROVIDERS = ("https://api64.ipify.org", "https://checkip.amazonaws.com")
_MAX_RESPONSE_BYTES = 64
_REQUEST_TIMEOUT = 2.0
_LOOKUP_TIMEOUT = 5.0
_CACHE_TTL = 45.0
_FAILURE = "현재 외부 공인 IP를 확인하지 못했습니다. 잠시 후 IP 명령으로 다시 확인하세요."


class PublicIPError(RuntimeError):
    """A local diagnostic without raw transport errors or response contents."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _address(raw: bytes) -> str:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_RESPONSE_BYTES:
        raise PublicIPError(_FAILURE)
    try:
        value = raw.decode("ascii").strip()
        if not value or "%" in value:
            raise ValueError
        address = ipaddress.ip_address(value)
        if not address.is_global or address.is_multicast or address.is_unspecified:
            raise ValueError
    except (ValueError, UnicodeError):
        raise PublicIPError(_FAILURE) from None
    return address.compressed


@dataclass
class _Lookup:
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    address: str | None = None


class PublicIPLookup:
    """One in-memory lookup at a time; callers never wait past its deadline.

    A daemon worker keeps queue congestion or OS DNS delays from blocking a
    command indefinitely. A queued lookup checks its deadline immediately before
    transport, so a timed-out, still-pending request cannot send later. There is
    no background polling and no cached failure or stale-success fallback.
    """

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, str] | None = None
        self._inflight: _Lookup | None = None

    def get(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            now = self._clock()
            if (not force_refresh and self._cached is not None
                    and 0 <= now - self._cached[0] < _CACHE_TTL):
                return self._cached[1]
            task = self._inflight
            create = task is None
            if create:
                task = _Lookup(now + _LOOKUP_TIMEOUT)
                self._inflight = task
        if create:
            worker = threading.Thread(target=self._discover, args=(task,), name="public-ip-lookup", daemon=True)
            try:
                worker.start()
            except Exception:
                with self._lock:
                    if self._inflight is task:
                        self._inflight = None
                raise PublicIPError(_FAILURE) from None
        remaining = max(0.0, min(_LOOKUP_TIMEOUT, task.deadline - self._clock()))
        if not task.done.wait(remaining) or task.address is None:
            raise PublicIPError(_FAILURE)
        return task.address

    def _discover(self, task: _Lookup) -> None:
        result = None
        try:
            for url in _PROVIDERS:
                remaining = task.deadline - self._clock()
                if remaining <= 0:
                    break
                def before_send():
                    if self._clock() >= task.deadline:
                        raise PublicIPError(_FAILURE)
                try:
                    opener = urllib.request.build_opener(_RejectRedirects())
                    request = urllib.request.Request(url, method="GET", headers={"Accept": "text/plain"})
                    with queued_open(opener, request, timeout=min(_REQUEST_TIMEOUT, remaining),
                                     max_bytes=_MAX_RESPONSE_BYTES, before_send=before_send) as response:
                        raw = response.read(_MAX_RESPONSE_BYTES + 1)
                        if getattr(response, "status", 200) != 200:
                            raise PublicIPError(_FAILURE)
                    result = _address(raw)
                    break
                except Exception:
                    # Only the two fixed providers are attempted, once each.
                    # No URL, response body or transport exception is exposed.
                    continue
        finally:
            with self._lock:
                completed_at = self._clock()
                if result is not None and completed_at < task.deadline:
                    task.address = result
                    self._cached = (completed_at, result)
                if self._inflight is task:
                    self._inflight = None
                task.done.set()


_lookup = PublicIPLookup()


def get_public_ip(*, force_refresh: bool = False) -> str:
    return _lookup.get(force_refresh=force_refresh)


def format_public_ip(address: str) -> str:
    value = ipaddress.ip_address(address)
    return f"현재 외부 공인 IP (IPv{value.version}): {value.compressed}"


def annotate_ip_error(code, message: str) -> str:
    """Add a fresh IP only to the two workbook-confirmed broker error codes.

    Other codes leave the caller's locally composed safe message unchanged.
    Recognized IP failures replace arbitrary provider text with local wording.
    This helper never treats generic network failures as an IP change.
    """
    normalized = str(code) if type(code) is int else code if type(code) is str else None
    if normalized not in ("8010", "8050"):
        return message if isinstance(message, str) else "키움 요청이 실패했습니다."
    if normalized == "8010":
        reason = "토큰 발급 IP와 요청 IP가 다릅니다."
        action = "현재 연결 환경에서 login paper 또는 login real로 다시 로그인하세요."
    else:
        reason = "IP가 등록되지 않았습니다."
        action = "키움 REST API 홈페이지의 API 사용신청 화면에서 등록 IP를 확인하세요."
    try:
        current = format_public_ip(get_public_ip(force_refresh=True))
    except Exception:
        current = _FAILURE
    return (f"키움 IP 오류 (return_code={normalized}): {reason}\n{current}\n"
            f"{action}\n자동 재시도하지 않았습니다.")
