"""In-memory Kiwoom login and daily token renewal; no broker orders.

Only the paired Telegram command dispatcher should invoke these operations.
Tokens remain in this object and are excluded from Telegram result messages,
normal representations and redirected logs. An explicit interactive terminal
report may show the token, as requested by the user.
"""

from __future__ import annotations
from .errors import BrokerAPIError

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import http.client
import json
import re
import sys
import threading
from typing import Callable, TextIO
import urllib.error
import urllib.request
from kiwoom_autotrader.utils.http_queue import queued_open
from zoneinfo import ZoneInfo

from kiwoom_autotrader.utils.keychain import Credentials, KeychainError, load_mock_credentials
from kiwoom_autotrader.utils.real_credentials import load_real_credentials
from . import au10001


KST = ZoneInfo("Asia/Seoul")
_ORIGINS = {"paper": "https://mockapi.kiwoom.com", "real": "https://api.kiwoom.com"}
_LABELS = {"paper": "모의투자", "real": "실전투자"}
_MAX_RESPONSE_BYTES = 64 * 1024


class LoginSessionError(BrokerAPIError):
    """Safe user-facing failure without credentials or server descriptions."""


@dataclass(frozen=True)
class LoginCommand:
    action: str
    value: str


@dataclass(frozen=True)
class IssuedToken:
    token: str = field(repr=False)
    expires_dt: str | None = None

    def __post_init__(self):
        if (not isinstance(self.token, str) or not 1 <= len(self.token) <= 8192
                or not self.token.isascii()
                or any(not 33 <= ord(ch) <= 126 for ch in self.token)):
            raise LoginSessionError("키움 인증 응답에 유효한 접근토큰이 없습니다.")
        if self.expires_dt is not None:
            try:
                if not isinstance(self.expires_dt, str) or not re.fullmatch(r"[0-9]{14}", self.expires_dt):
                    raise ValueError
                datetime.strptime(self.expires_dt, "%Y%m%d%H%M%S")
            except ValueError:
                raise LoginSessionError("키움 인증 응답의 만료 시각이 올바르지 않습니다.") from None


@dataclass(frozen=True)
class LoginResult:
    success: bool
    mode: str
    renewal: bool
    message: str
    expires_dt: str | None = None


def normalize_mode(value: str) -> str:
    if not isinstance(value, str) or value.strip().casefold() not in _ORIGINS:
        raise ValueError("투자 구분은 paper 또는 real이어야 합니다.")
    return value.strip().casefold()


def normalize_renewal_time(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("갱신 시각은 24시간제 H:MM 또는 HH:MM으로 입력하세요.")
    match = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})", value.strip())
    if not match or not 0 <= int(match[1]) <= 23 or not 0 <= int(match[2]) <= 59:
        raise ValueError("갱신 시각은 00:00~23:59 범위의 H:MM 또는 HH:MM이어야 합니다.")
    return f"{int(match[1]):02d}:{int(match[2]):02d}"


def parse_login_command(text: str) -> LoginCommand | None:
    """Parse only login commands, case-insensitively with whitespace trimmed."""
    if not isinstance(text, str):
        return None
    words = text.strip().casefold().split()
    if not words or words[0] != "login":
        return None
    if len(words) == 2 and words[1] in _ORIGINS:
        return LoginCommand("login", words[1])
    if len(words) == 3 and words[1] == "renewal":
        return LoginCommand("renewal", normalize_renewal_time(words[2]))
    raise ValueError("사용법: login paper, login real, login renewal 8:55")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_token(mode: str, credentials: Credentials) -> IssuedToken:
    """Issue one token using a fixed origin; never print the raw response."""
    mode = normalize_mode(mode)
    if not isinstance(credentials, Credentials):
        raise LoginSessionError("키움 API 키를 확인하세요.")
    credentials.__post_init__()
    request = urllib.request.Request(
        _ORIGINS[mode] + au10001.ENDPOINT,
        data=json.dumps(au10001.request_body(credentials.app_key, credentials.app_secret),
                        ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json;charset=UTF-8"}, method="POST",
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with queued_open(opener, request, timeout=15, max_bytes=_MAX_RESPONSE_BYTES) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        code = error.code
        try:
            error.close()
        except OSError:
            pass
        if type(code) is int and 100 <= code <= 599:
            raise LoginSessionError(f"키움 인증 HTTP 오류 (code={code}).") from None
        raise LoginSessionError("키움 인증 요청에 실패했습니다.") from None
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        raise LoginSessionError("키움 인증 서버에 연결하지 못했습니다.") from None
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise LoginSessionError("키움 인증 응답 크기가 제한을 초과했습니다.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise LoginSessionError("키움 인증 응답 형식이 올바르지 않습니다.") from None
    if not isinstance(payload, dict):
        raise LoginSessionError("키움 인증 응답 형식이 올바르지 않습니다.")
    code = payload.get("return_code")
    if not ((type(code) is int and code == 0) or (type(code) is str and code == "0")):
        if type(code) is int or (type(code) is str and re.fullmatch(r"-?[0-9]{1,6}", code)):
            raise LoginSessionError(f"키움 인증 실패 (return_code={code}).")
        raise LoginSessionError("키움 인증 성공 여부를 확인하지 못했습니다.")
    if not isinstance(payload.get("token_type"), str) or payload["token_type"].casefold() != "bearer":
        raise LoginSessionError("키움 인증 응답의 토큰 종류가 올바르지 않습니다.")
    if not isinstance(payload.get("expires_dt"), str) or not payload["expires_dt"]:
        raise LoginSessionError("키움 인증 응답의 만료 시각이 올바르지 않습니다.")
    return IssuedToken(payload.get("token"), payload.get("expires_dt"))


def load_credentials(mode: str) -> Credentials | None:
    return load_mock_credentials() if normalize_mode(mode) == "paper" else load_real_credentials()


def _local_now() -> datetime:
    return datetime.now(KST)


class BrokerSession:
    def __init__(
        self, renewal_time: str = "08:55", *, now: Callable[[], datetime] = _local_now,
        credential_loader: Callable[[str], Credentials | None] | None = None,
        token_issuer: Callable[[str, Credentials], IssuedToken] | None = None,
        save_renewal: Callable[[str], None] | None = None,
    ):
        self.renewal_time = normalize_renewal_time(renewal_time)
        self._now = now
        self._credential_loader = credential_loader or load_credentials
        self._token_issuer = token_issuer or request_token
        self._save_renewal = save_renewal
        self._auth_lock = threading.RLock()
        self.mode: str | None = None
        self._issued: IssuedToken | None = None
        self.next_renewal_at: datetime | None = None

    def __repr__(self):
        return f"BrokerSession(mode={self.mode!r}, authenticated={self.authenticated}, renewal_time={self.renewal_time!r})"

    @property
    def token(self) -> str | None:
        return self._issued.token if self._issued else None

    @property
    def expires_dt(self) -> str | None:
        return self._issued.expires_dt if self._issued else None

    @property
    def authenticated(self) -> bool:
        """Whether a login session exists, including one awaiting renewal."""
        return self.mode is not None and self._issued is not None

    @property
    def token_usable(self) -> bool:
        """Check expiry using the application's KST interpretation of expires_dt.

        Kiwoom documents a string expiry without an explicit timezone. The
        application treats its calendar value as KST. Real HTTP responses must
        supply it; injectable test issuers may omit it.
        """
        if not self.authenticated:
            return False
        return self.expires_dt is None or self._time() < self._expiry(self.expires_dt)

    @staticmethod
    def _expiry(value: str) -> datetime:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=KST)

    def _time(self, value: datetime | None = None) -> datetime:
        value = self._now() if value is None else value
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("토큰 갱신 시계에는 시간대가 포함되어야 합니다.")
        return value.astimezone(KST)

    def _next(self, now: datetime) -> datetime:
        hour, minute = map(int, self.renewal_time.split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return target + timedelta(days=1) if target <= now else target

    def _issue(self, mode: str, renewal: bool, now: datetime) -> LoginResult:
        # A token transition is indivisible to read-only research snapshots.
        # Normal trading paths do not acquire this lock; research fails fast
        # instead of delaying the shared HTTP dispatcher while login is active.
        with self._auth_lock:
            return self._issue_locked(mode, renewal, now)

    def _issue_locked(self, mode: str, renewal: bool, now: datetime) -> LoginResult:
        action = "토큰 갱신" if renewal else "로그인"
        label = _LABELS[mode]
        try:
            credentials = self._credential_loader(mode)
            if credentials is None:
                setup = "setup-mock.command" if mode == "paper" else "setup-real.command"
                raise LoginSessionError(f"{label} API 키가 저장되지 않았습니다. Mac에서 {setup}를 실행하세요.")
            issued = self._token_issuer(mode, credentials)
            if not isinstance(issued, IssuedToken):
                raise LoginSessionError("키움 인증 응답의 접근토큰을 확인하지 못했습니다.")
            issued.__post_init__()
            # Authentication can cross the configured renewal boundary. Base
            # the next deadline on completion, or a fresh token would be renewed
            # immediately on the next receiver iteration. Never move behind the
            # request's reference time if the wall clock moves backwards.
            completed_at = max(now, self._time())
            if issued.expires_dt is not None and self._expiry(issued.expires_dt) <= completed_at:
                raise LoginSessionError("키움 인증 응답의 접근토큰이 이미 만료되었습니다.")
        except Exception as error:
            reason = str(error) if isinstance(error, LoginSessionError) else "API 키·등록 IP·네트워크·키체인 상태를 확인하세요."
            # Only module-defined safe errors may enter command replies.
            if self.authenticated and self.token_usable:
                retained = f" 기존 {_LABELS[self.mode]} 로그인 상태를 유지합니다."
            elif self.authenticated:
                retained = f" 기존 {_LABELS[self.mode]} 접근토큰은 만료되어 다시 로그인해야 합니다."
            else:
                retained = " 현재 로그인되지 않았습니다."
            return LoginResult(False, mode, renewal, f"{label} {action} 실패. {reason}{retained}")
        self.mode, self._issued = mode, issued
        self.next_renewal_at = self._next(completed_at)
        expiry = f" 만료 시각(KST): {issued.expires_dt}." if issued.expires_dt else ""
        return LoginResult(True, mode, renewal,
                           f"{label} {action} 성공. 매일 {self.renewal_time}(한국 시간)에 갱신합니다.{expiry}",
                           issued.expires_dt)

    def login(self, mode: str) -> LoginResult:
        return self._issue(normalize_mode(mode), False, self._time())

    def set_renewal(self, value: str, *, persist: bool = True) -> str:
        normalized = normalize_renewal_time(value)
        if persist and self._save_renewal is not None:
            try:
                self._save_renewal(normalized)
            except Exception:
                raise LoginSessionError("갱신 시각 설정을 저장하지 못했습니다. 기존 시각을 유지합니다.") from None
        self.renewal_time = normalized
        if self.authenticated:
            self.next_renewal_at = self._next(self._time())
        return normalized

    def seconds_until_renewal(self, now: datetime | None = None) -> float | None:
        if not self.authenticated or self.next_renewal_at is None:
            return None
        return max(0.0, (self.next_renewal_at - self._time(now)).total_seconds())

    def renew_if_due(self, now: datetime | None = None) -> LoginResult | None:
        current = self._time(now)
        if not self.authenticated or self.next_renewal_at is None or current < self.next_renewal_at:
            return None
        # Advance before the request. A failed scheduled attempt is reported once,
        # not retried on every long-poll iteration; manual login can retry anytime.
        self.next_renewal_at = self._next(current)
        return self._issue(self.mode, True, current)

    def clear(self) -> None:
        with self._auth_lock:
            self.mode = self._issued = self.next_renewal_at = None


def terminal_report(result: LoginResult, session: BrokerSession, stream: TextIO | None = None) -> None:
    stream = stream or sys.stdout
    print(result.message, file=stream, flush=True)
    if result.success:
        try:
            interactive = stream.isatty()
        except (AttributeError, OSError):
            interactive = False
        value = session.token if interactive else "[비공개: 토큰은 실행 중 메모리에 보관됨]"
        print("접근토큰: " + (value or "없음"), file=stream, flush=True)
