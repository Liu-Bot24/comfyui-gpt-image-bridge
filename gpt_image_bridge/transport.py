from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from typing import Any, Callable

from .errors import BridgeError, redact_text, redact_url


MAX_RESPONSE_BYTES = 80 * 1024 * 1024
MAX_ERROR_BYTES = 1024 * 1024
RETRY_STATUSES = {429, 500, 502, 503, 504}
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def _is_public_or_fake_ip(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.is_global or (
        isinstance(parsed, ipaddress.IPv4Address) and parsed in _FAKE_IP_NETWORK
    )


def _validate_public_download_url(
    url: str,
    *,
    resolver: Callable[..., Any] = socket.getaddrinfo,
    resolve_dns: bool = True,
) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise BridgeError(
            "Provider returned an invalid image URL.",
            error_code="invalid_image_url",
        ) from None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise BridgeError(
            "Provider returned an invalid image URL.",
            error_code="invalid_image_url",
        )
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.startswith("["):
        try:
            ipaddress.IPv6Address(parsed.hostname)
        except ValueError:
            raise BridgeError(
                "Provider returned an invalid image URL.",
                error_code="invalid_image_url",
            ) from None
    if parsed.username is not None or parsed.password is not None:
        raise BridgeError(
            "Provider returned an image URL containing user information.",
            error_code="unsafe_image_url",
        )
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise BridgeError(
            "Provider returned a local image URL, which was blocked.",
            error_code="unsafe_image_url",
        )
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_or_fake_ip(hostname):
            raise BridgeError(
                "Provider returned a non-public image URL, which was blocked.",
                error_code="unsafe_image_url",
            )
        return
    if not resolve_dns:
        return
    try:
        addresses = resolver(
            hostname,
            port or (443 if parsed.scheme.lower() == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (OSError, ValueError):
        # A configured HTTP proxy may resolve the hostname remotely.
        return
    for entry in addresses:
        address = entry[4][0]
        if not _is_public_or_fake_ip(address):
            raise BridgeError(
                "Provider returned an image URL resolving to a non-public address, "
                "which was blocked.",
                error_code="unsafe_image_url",
            )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, resolver: Callable[..., Any] = socket.getaddrinfo) -> None:
        super().__init__()
        self._resolver = resolver

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute_url = urllib.parse.urljoin(req.full_url, newurl)
        if req.get_header("Authorization"):
            raise BridgeError(
                "Authenticated API redirects are refused. Configure the final endpoint "
                "directly so the Authorization header cannot leave its intended request.",
                error_code="authenticated_redirect_refused",
                endpoint=redact_url(absolute_url),
                retryable=False,
            )
        if req.get_method().upper() not in {"GET", "HEAD"}:
            raise BridgeError(
                "API request redirects are refused. Configure the final endpoint directly "
                "so prompts and images are not sent to an unexpected target.",
                error_code="request_redirect_refused",
                endpoint=redact_url(absolute_url),
                retryable=False,
            )
        public_download = bool(
            getattr(req, "_gpt_image_bridge_public_download", False)
        )
        if public_download:
            _validate_public_download_url(
                absolute_url,
                resolver=self._resolver,
                resolve_dns=True,
            )
        redirected = super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            absolute_url,
        )
        if redirected is not None and public_download:
            setattr(redirected, "_gpt_image_bridge_public_download", True)
        return redirected


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    attempts: int
    url: str

    @property
    def request_id(self) -> str | None:
        for name in ("x-request-id", "request-id", "openai-request-id"):
            if self.headers.get(name):
                return self.headers[name]
        return None


def _headers_dict(headers: Message | Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _redact_optional(value: Any, known_secrets: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    return redact_text(value, known_secrets)


def _redact_bridge_error(
    error: BridgeError,
    known_secrets: tuple[str, ...],
) -> None:
    """Sanitize errors raised below urllib before they leave the transport."""
    for name in (
        "message",
        "request_id",
        "error_type",
        "error_code",
        "moderation_category",
        "retry_after",
        "operation",
        "protocol",
        "endpoint",
        "phase",
    ):
        value = getattr(error, name)
        if isinstance(value, str):
            setattr(error, name, redact_text(value, known_secrets))


def _read_limited(response: Any, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise BridgeError(
            f"HTTP response exceeded the {limit}-byte safety limit.",
            error_code="response_too_large",
        )
    return data


def _retry_delay(headers: dict[str, str], attempt: int) -> float:
    retry_after = headers.get("retry-after", "").strip()
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 10.0)
        except ValueError:
            pass
    return min(0.5 * (2 ** max(0, attempt - 1)), 4.0)


def _error_fields(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        text = body.decode("utf-8", errors="replace").strip()
        return {"message": text[:2000] or "HTTP request failed."}
    error = payload.get("error", payload)
    if not isinstance(error, dict):
        return {"message": str(error)[:2000]}
    nested = error.get("details")
    if not isinstance(nested, dict):
        nested = {}
    return {
        "message": error.get("message") or error.get("detail") or "HTTP request failed.",
        "error_type": error.get("type"),
        "error_code": error.get("code"),
        "moderation_category": (
            error.get("moderation_category")
            or error.get("category")
            or nested.get("moderation_category")
            or nested.get("category")
        ),
    }


class HttpClient:
    def __init__(
        self,
        *,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        resolver: Callable[..., Any] = socket.getaddrinfo,
    ) -> None:
        self._uses_default_opener = opener is None
        self._opener = (
            urllib.request.build_opener(_SafeRedirectHandler(resolver)).open
            if opener is None
            else opener
        )
        self._sleep = sleeper
        self._clock = clock
        self._resolver = resolver

    def monotonic(self) -> float:
        """Return the client's monotonic clock for a shared operation deadline."""
        return self._clock()

    def sleep(self, seconds: float) -> None:
        """Sleep through the injected scheduler used by transport tests."""
        self._sleep(max(0.0, float(seconds)))

    def request(
        self,
        *,
        url: str,
        method: str = "POST",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 300,
        max_attempts: int = 3,
        known_secrets: tuple[str, ...] = (),
        operation: str | None = None,
        protocol: str | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        public_url_only: bool = False,
    ) -> HttpResponse:
        if public_url_only:
            _validate_public_download_url(
                url,
                resolver=self._resolver,
                resolve_dns=self._uses_default_opener,
            )
        safe_url = redact_url(url, known_secrets)
        started = self._clock()
        last_error: BridgeError | None = None
        attempts = max(1, int(max_attempts))
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(
                url,
                data=body,
                headers=headers or {},
                method=method,
            )
            if public_url_only:
                setattr(request, "_gpt_image_bridge_public_download", True)
            try:
                with self._opener(request, timeout=int(timeout)) as response:
                    response_headers = {
                        name: redact_text(value, known_secrets)
                        for name, value in _headers_dict(response.headers).items()
                    }
                    payload = _read_limited(response, max_response_bytes)
                    return HttpResponse(
                        status=int(getattr(response, "status", response.getcode())),
                        headers=response_headers,
                        body=payload,
                        elapsed_ms=round((self._clock() - started) * 1000),
                        attempts=attempt,
                        url=safe_url,
                    )
            except BridgeError as error:
                if error.operation is None:
                    error.operation = operation
                if error.protocol is None:
                    error.protocol = protocol
                if error.endpoint is None:
                    error.endpoint = safe_url
                error.attempts = max(error.attempts, attempt)
                _redact_bridge_error(error, known_secrets)
                raise
            except urllib.error.HTTPError as error:
                response_headers = {
                    name: redact_text(value, known_secrets)
                    for name, value in _headers_dict(error.headers or {}).items()
                }
                error_body = error.read(MAX_ERROR_BYTES)
                fields = _error_fields(error_body)
                status = int(error.code)
                retryable = status in RETRY_STATUSES
                last_error = BridgeError(
                    redact_text(fields["message"], known_secrets),
                    status=status,
                    request_id=(
                        response_headers.get("x-request-id")
                        or response_headers.get("request-id")
                        or response_headers.get("openai-request-id")
                    ),
                    error_type=_redact_optional(fields.get("error_type"), known_secrets),
                    error_code=_redact_optional(fields.get("error_code"), known_secrets),
                    moderation_category=_redact_optional(
                        fields.get("moderation_category"), known_secrets
                    ),
                    retry_after=response_headers.get("retry-after"),
                    operation=operation,
                    protocol=protocol,
                    endpoint=safe_url,
                    retryable=retryable,
                    attempts=attempt,
                )
                if retryable and attempt < attempts:
                    self._sleep(_retry_delay(response_headers, attempt))
                    continue
                raise last_error from None
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as error:
                last_error = BridgeError(
                    redact_text(f"Network transport failed: {error}", known_secrets),
                    error_type="network",
                    error_code="network_error",
                    operation=operation,
                    protocol=protocol,
                    endpoint=safe_url,
                    retryable=True,
                    attempts=attempt,
                )
                if attempt < attempts:
                    self._sleep(_retry_delay({}, attempt))
                    continue
                raise last_error from None
        assert last_error is not None
        raise last_error
