from __future__ import annotations

import base64
import errno
import json
import os
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .errors import BridgeError


CODEX_API_BASE_URL = "https://chatgpt.com/backend-api/codex"
OPENAI_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_EXPIRY_MARGIN = timedelta(minutes=5)
_REFRESH_INTERVAL = timedelta(minutes=55)
_AUTH_FILE_MAX_BYTES = 1024 * 1024
_TOKEN_RESPONSE_MAX_BYTES = 1024 * 1024
_AUTH_RELOAD_LIMIT = 4
_AUTH_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class OAuthSession:
    access_token: str = field(repr=False)
    account_id: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    id_token: str | None = field(default=None, repr=False)
    is_fedramp: bool = False
    source_path: Path = field(default=Path(), repr=False)
    last_refresh: str | None = field(default=None, repr=False)


def _authentication_error(
    message: str,
    *,
    error_code: str,
    status: int | None = None,
) -> BridgeError:
    return BridgeError(
        message,
        status=status,
        error_type="authentication",
        error_code=error_code,
        retryable=False,
    )


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError:
        raise _authentication_error(
            "Codex OAuth credentials could not be read.",
            error_code="codex_auth_read_failed",
        ) from None


def preferred_codex_auth_file() -> Path | None:
    standard = Path.home() / ".codex" / "auth.json"
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        candidate = Path(codex_home).expanduser() / "auth.json"
        if _is_regular_file(candidate):
            return candidate
    if _is_regular_file(standard):
        return standard
    return None


def _read_auth_file(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _AUTH_FILE_MAX_BYTES:
            raise ValueError("oversized")
        raw = path.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _authentication_error(
            "Codex OAuth credentials are unreadable or malformed. "
            "Run `codex login` and retry.",
            error_code="codex_auth_invalid",
        ) from None
    if not isinstance(parsed, dict):
        raise _authentication_error(
            "Codex OAuth credentials are unreadable or malformed. "
            "Run `codex login` and retry.",
            error_code="codex_auth_invalid",
        )
    return parsed


def _auth_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


@contextmanager
def _interprocess_auth_lock(path: Path) -> Iterator[None]:
    descriptor: int | None = None
    locked = False
    try:
        try:
            descriptor = os.open(
                _auth_lock_path(path),
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                while True:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    try:
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError as error:
                        if error.errno not in {errno.EACCES, errno.EDEADLK}:
                            raise
                        time.sleep(0.05)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
                locked = True
        except OSError:
            raise _authentication_error(
                "Codex OAuth credentials could not be locked.",
                error_code="codex_auth_lock_failed",
            ) from None
        yield
    finally:
        if descriptor is not None:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(descriptor)
            except OSError:
                pass


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _jwt_claims(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3 or not parts[1] or len(parts[1]) > 128 * 1024:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        parsed = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _derive_account_id(token: str | None) -> str | None:
    claims = _jwt_claims(token)
    if claims is None:
        return None

    auth_claim = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claim, dict):
        account_id = _string_value(auth_claim.get("chatgpt_account_id"))
        if account_id:
            return account_id

    account_id = _string_value(claims.get("chatgpt_account_id"))
    if account_id:
        return account_id

    organizations = claims.get("organizations")
    if isinstance(organizations, list):
        for organization in organizations:
            if isinstance(organization, dict):
                account_id = _string_value(organization.get("id"))
                if account_id:
                    return account_id
    return None


def _derive_fedramp(token: str | None) -> bool:
    claims = _jwt_claims(token)
    if claims is None:
        return False

    auth_claim = claims.get("https://api.openai.com/auth")
    if (
        isinstance(auth_claim, dict)
        and auth_claim.get("chatgpt_account_is_fedramp") is True
    ):
        return True
    if claims.get("chatgpt_account_is_fedramp") is True:
        return True

    organizations = claims.get("organizations")
    if isinstance(organizations, list):
        for organization in organizations:
            if not isinstance(organization, dict):
                continue
            if (
                organization.get("chatgpt_account_is_fedramp") is True
                or organization.get("is_fedramp") is True
            ):
                return True
    return False


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return _as_utc(datetime.fromisoformat(normalized))
    except ValueError:
        return None


def _format_iso_datetime(value: datetime) -> str:
    return (
        _as_utc(value)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _should_refresh(
    access_token: str | None,
    last_refresh: Any,
    now: datetime,
) -> bool:
    if not access_token:
        return True

    claims = _jwt_claims(access_token)
    expires_at: datetime | None = None
    if claims is not None:
        exp = claims.get("exp")
        if isinstance(exp, (int, float)) and not isinstance(exp, bool):
            try:
                expires_at = datetime.fromtimestamp(float(exp), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                expires_at = None
    if expires_at is not None and expires_at <= now + _EXPIRY_MARGIN:
        return True

    refreshed_at = _parse_iso_datetime(last_refresh)
    return (
        refreshed_at is not None
        and refreshed_at <= now - _REFRESH_INTERVAL
    )


def _refresh_tokens(refresh_token: str, timeout: float) -> dict[str, Any]:
    request_body = json.dumps(
        {
            "client_id": OPENAI_OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        OPENAI_OAUTH_TOKEN_URL,
        data=request_body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = getattr(response, "status", None)
            if status_code is None:
                status_code = response.getcode()
            response_body = response.read(_TOKEN_RESPONSE_MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise _authentication_error(
            "Codex OAuth credentials could not be refreshed.",
            error_code="codex_oauth_refresh_failed",
            status=error.code,
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise _authentication_error(
            "Codex OAuth credentials could not be refreshed.",
            error_code="codex_oauth_refresh_failed",
        ) from None

    if not 200 <= int(status_code) < 300:
        raise _authentication_error(
            "Codex OAuth credentials could not be refreshed.",
            error_code="codex_oauth_refresh_failed",
            status=int(status_code),
        )
    if len(response_body) > _TOKEN_RESPONSE_MAX_BYTES:
        raise _authentication_error(
            "Codex OAuth credentials could not be refreshed.",
            error_code="codex_oauth_refresh_invalid",
            status=int(status_code),
        )
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _authentication_error(
            "Codex OAuth credentials could not be refreshed.",
            error_code="codex_oauth_refresh_invalid",
            status=int(status_code),
        ) from None
    if not isinstance(payload, dict) or not _string_value(payload.get("access_token")):
        raise _authentication_error(
            "Codex OAuth credentials could not be refreshed.",
            error_code="codex_oauth_refresh_invalid",
            status=int(status_code),
        )
    return payload


def _write_auth_file_atomic(
    path: Path,
    payload: dict[str, Any],
    *,
    expected_auth: dict[str, Any],
) -> bool:
    temporary_path: Path | None = None
    descriptor: int | None = None
    try:
        if _read_auth_file(path) != expected_auth:
            return False
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        temporary_path = Path(raw_path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if _read_auth_file(path) != expected_auth:
            return False
        os.replace(temporary_path, path)
        temporary_path = None
        return True
    except OSError:
        raise _authentication_error(
            "Refreshed Codex OAuth credentials could not be saved.",
            error_code="codex_auth_write_failed",
        ) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _session_from_auth(
    path: Path,
    auth: dict[str, Any],
    *,
    timeout: float,
    now: datetime,
) -> OAuthSession:
    for _reload_attempt in range(_AUTH_RELOAD_LIMIT):
        tokens = auth.get("tokens")
        if not isinstance(tokens, dict):
            raise _authentication_error(
                "Codex OAuth credentials are incomplete. Run `codex login` and retry.",
                error_code="codex_auth_incomplete",
            )

        access_token = _string_value(tokens.get("access_token"))
        refresh_token = _string_value(tokens.get("refresh_token"))
        id_token = _string_value(tokens.get("id_token"))
        account_id = _string_value(tokens.get("account_id"))
        account_id = (
            account_id
            or _derive_account_id(id_token)
            or _derive_account_id(access_token)
        )
        is_fedramp = _derive_fedramp(id_token) or _derive_fedramp(access_token)
        last_refresh = _string_value(auth.get("last_refresh"))

        if _should_refresh(access_token, last_refresh, now):
            guarded_auth = _read_auth_file(path)
            if guarded_auth != auth:
                auth = guarded_auth
                continue
            if refresh_token is None:
                raise _authentication_error(
                    "Codex OAuth credentials have expired and cannot be refreshed. "
                    "Run `codex login` and retry.",
                    error_code="codex_oauth_refresh_token_missing",
                )
            refreshed = _refresh_tokens(refresh_token, timeout)
            guarded_auth = _read_auth_file(path)
            if guarded_auth != auth:
                auth = guarded_auth
                continue

            access_token = _string_value(refreshed.get("access_token"))
            assert access_token is not None
            refresh_token = (
                _string_value(refreshed.get("refresh_token")) or refresh_token
            )
            id_token = _string_value(refreshed.get("id_token")) or id_token
            account_id = (
                _string_value(refreshed.get("account_id"))
                or _derive_account_id(id_token)
                or _derive_account_id(access_token)
                or account_id
            )
            is_fedramp = (
                is_fedramp
                or _derive_fedramp(id_token)
                or _derive_fedramp(access_token)
            )
            last_refresh = _format_iso_datetime(now)

            updated_auth = dict(auth)
            updated_tokens = dict(tokens)
            updated_tokens["access_token"] = access_token
            updated_tokens["refresh_token"] = refresh_token
            if id_token is not None:
                updated_tokens["id_token"] = id_token
            if account_id is not None:
                updated_tokens["account_id"] = account_id
            updated_auth["tokens"] = updated_tokens
            updated_auth["last_refresh"] = last_refresh
            if not _write_auth_file_atomic(
                path,
                updated_auth,
                expected_auth=auth,
            ):
                auth = _read_auth_file(path)
                continue

        if access_token is None:
            raise _authentication_error(
                "Codex OAuth credentials are incomplete. Run `codex login` and retry.",
                error_code="codex_access_token_missing",
            )
        if account_id is None:
            raise _authentication_error(
                "Codex OAuth credentials are incomplete. Run `codex login` and retry.",
                error_code="codex_account_id_missing",
            )
        return OAuthSession(
            access_token=access_token,
            account_id=account_id,
            refresh_token=refresh_token,
            id_token=id_token,
            is_fedramp=is_fedramp,
            source_path=path,
            last_refresh=last_refresh,
        )
    raise _authentication_error(
        "Codex OAuth credentials changed repeatedly while being refreshed. Retry.",
        error_code="codex_auth_changed",
    )


def load_oauth_session(
    timeout: float = 30,
    now: datetime | None = None,
) -> OAuthSession:
    timeout = max(1.0, float(timeout))
    current_time = _as_utc(now or datetime.now(timezone.utc))
    with _AUTH_LOCK:
        auth_file = preferred_codex_auth_file()
        if auth_file is None:
            raise _authentication_error(
                "Codex OAuth credentials were not found. Run `codex login` and retry.",
                error_code="codex_auth_missing",
            )
        with _interprocess_auth_lock(auth_file):
            auth = _read_auth_file(auth_file)
            return _session_from_auth(
                auth_file,
                auth,
                timeout=timeout,
                now=current_time,
            )


def oauth_request_context(
    timeout: float = 30,
) -> tuple[str, dict[str, str], tuple[str, ...]]:
    session = load_oauth_session(timeout=timeout)
    headers = {
        "Authorization": f"Bearer {session.access_token}",
        "chatgpt-account-id": session.account_id,
    }
    if session.is_fedramp:
        headers["X-OpenAI-Fedramp"] = "true"
    secrets = tuple(
        dict.fromkeys(
            secret
            for secret in (
                session.access_token,
                session.refresh_token,
                session.id_token,
                session.account_id,
            )
            if secret
        )
    )
    return CODEX_API_BASE_URL, headers, secrets
