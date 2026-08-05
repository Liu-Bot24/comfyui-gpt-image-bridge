from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
}
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_KEYISH_RE = re.compile(r"\b(?:sk|sess|key)-[A-Za-z0-9._-]{8,}\b", re.IGNORECASE)
_DATA_URL_RE = re.compile(
    r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+",
    re.IGNORECASE,
)
_LONG_B64_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{256,}={0,2}(?![A-Za-z0-9+/=])")
_HTTP_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)


def _mask_url_query_match(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;!?)":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw + trailing
    if not parsed.query:
        return raw + trailing
    masked = "&".join(
        f"{component.split('=', 1)[0]}=[REDACTED]"
        for component in parsed.query.split("&")
    )
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, masked, "")
    ) + trailing


def redact_text(value: Any, known_secrets: tuple[str, ...] = ()) -> str:
    text = str(value)
    for secret in known_secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _KEYISH_RE.sub("[REDACTED]", text)
    text = _DATA_URL_RE.sub("data:image/[REDACTED];base64,[REDACTED]", text)
    text = _HTTP_URL_RE.sub(_mask_url_query_match, text)
    text = _LONG_B64_RE.sub("[REDACTED_BASE64]", text)
    return text


def redact_url(value: Any, known_secrets: tuple[str, ...] = ()) -> str:
    text = str(value)
    try:
        parsed = urlsplit(text)
    except ValueError:
        return redact_text(text, known_secrets)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return redact_text(text, known_secrets)
    query = ""
    if parsed.query:
        components = []
        for component in parsed.query.split("&"):
            name = component.split("=", 1)[0]
            components.append(f"{redact_text(name, known_secrets)}=[REDACTED]")
        query = "&".join(components)
    return redact_text(
        urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, "")),
        known_secrets,
    )


def sanitize(value: Any, known_secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                cleaned[str(key)] = "[REDACTED]"
            else:
                cleaned[str(key)] = sanitize(item, known_secrets)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [sanitize(item, known_secrets) for item in value]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        return redact_text(value, known_secrets)
    return value


@dataclass
class BridgeError(RuntimeError):
    message: str
    status: int | None = None
    request_id: str | None = None
    error_type: str | None = None
    error_code: str | None = None
    moderation_category: str | None = None
    retry_after: str | None = None
    operation: str | None = None
    protocol: str | None = None
    endpoint: str | None = None
    retryable: bool = False
    attempts: int = 1
    phase: str | None = None
    safe_to_resubmit: bool | None = None
    remote_task_may_continue: bool | None = None

    def report(self, known_secrets: tuple[str, ...] = ()) -> dict[str, Any]:
        return sanitize(
            {
                "ok": False,
                "message": self.message,
                "http_status": self.status,
                "request_id": self.request_id,
                "error_type": self.error_type,
                "error_code": self.error_code,
                "moderation_category": self.moderation_category,
                "retry_after": self.retry_after,
                "operation": self.operation,
                "protocol": self.protocol,
                "endpoint": self.endpoint,
                "retryable": self.retryable,
                "attempts": self.attempts,
                "phase": self.phase,
                "safe_to_resubmit": self.safe_to_resubmit,
                "remote_task_may_continue": self.remote_task_may_continue,
            },
            known_secrets,
        )

    def __str__(self) -> str:
        return json.dumps(self.report(), ensure_ascii=False, separators=(",", ":"))


def report_json(report: dict[str, Any], known_secrets: tuple[str, ...] = ()) -> str:
    return json.dumps(
        sanitize(report, known_secrets),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
