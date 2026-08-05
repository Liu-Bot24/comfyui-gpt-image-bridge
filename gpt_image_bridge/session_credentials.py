from __future__ import annotations

import secrets
import re
import threading
import time

from .errors import BridgeError


HANDLE_PREFIX = "gpt-image-bridge-session:"
SESSION_TTL_SECONDS = 10 * 60
MAX_SESSION_CREDENTIALS = 128

_LOCK = threading.Lock()
_CREDENTIALS: dict[str, tuple[float, str]] = {}
_HANDLE_PATTERN = re.compile(
    rf"^{re.escape(HANDLE_PREFIX)}[A-Za-z0-9_-]{{43}}$"
)


def _purge_expired(now: float) -> None:
    expired = [
        handle
        for handle, (expires_at, _secret) in _CREDENTIALS.items()
        if expires_at <= now
    ]
    for handle in expired:
        _CREDENTIALS.pop(handle, None)


def issue_session_credential(api_key: str, *, now: float | None = None) -> str:
    """Store an API key in process memory and return a non-secret queue handle."""
    secret = str(api_key or "").strip()
    if not secret:
        raise ValueError("api_key cannot be empty.")
    if len(secret) > 16 * 1024:
        raise ValueError("api_key is too large.")

    current = time.monotonic() if now is None else float(now)
    handle = HANDLE_PREFIX + secrets.token_urlsafe(32)
    with _LOCK:
        _purge_expired(current)
        while len(_CREDENTIALS) >= MAX_SESSION_CREDENTIALS:
            oldest = min(_CREDENTIALS, key=lambda item: _CREDENTIALS[item][0])
            _CREDENTIALS.pop(oldest, None)
        _CREDENTIALS[handle] = (current + SESSION_TTL_SECONDS, secret)
    return handle


def is_session_handle(value: str) -> bool:
    return _HANDLE_PATTERN.fullmatch(str(value or "").strip()) is not None


def consume_session_credential(
    value: str,
    *,
    now: float | None = None,
    require_handle: bool = False,
) -> str:
    """Consume a UI session handle, while preserving direct API-client compatibility."""
    candidate = str(value or "").strip()
    if not candidate.startswith(HANDLE_PREFIX):
        if require_handle and candidate:
            raise BridgeError(
                "The API Key must be submitted through the protected node widget. "
                "Reload ComfyUI so the GPT Image Bridge frontend extension is active, "
                "then re-enter the Key and queue again.",
                error_type="authentication",
                error_code="session_credential_required",
            )
        return candidate

    current = time.monotonic() if now is None else float(now)
    with _LOCK:
        _purge_expired(current)
        entry = _CREDENTIALS.pop(candidate, None)
    if entry is None:
        raise BridgeError(
            "The temporary API credential expired or is unavailable. "
            "Re-enter the API Key in GPT Image Bridge · API Provider and queue again.",
            error_type="authentication",
            error_code="session_credential_missing",
        )
    return entry[1]


def clear_session_credentials() -> None:
    """Clear volatile credentials; intended for shutdown paths and tests."""
    with _LOCK:
        _CREDENTIALS.clear()
