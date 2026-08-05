from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from .errors import BridgeError
from .session_credentials import consume_session_credential


AUTH_MODE_API = "api_key"
AUTH_MODE_OAUTH = "codex_oauth"
AUTH_MODES = (AUTH_MODE_API, AUTH_MODE_OAUTH)
API_PROTOCOLS = ("auto", "responses", "images", "chat_completions")
OAUTH_PROTOCOLS = ("auto", "responses", "images")
# Backward-compatible export retains the original three choices.
PROTOCOLS = OAUTH_PROTOCOLS
DEFAULT_API_BASE_URL = "https://api.openai.com/v1"
_SENSITIVE_ENDPOINT_QUERY_NAMES = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "code",
    "credential",
    "key",
    "password",
    "secret",
    "signature",
    "sig",
    "token",
}
_ASYNC_MAPPING_POINTER_KEYS = {
    "task_id_paths",
    "status_paths",
    "result_paths",
    "image_items_paths",
    "image_b64_paths",
    "image_url_paths",
    "revised_prompt_paths",
    "error_message_paths",
    "error_type_paths",
    "error_code_paths",
}
_ASYNC_MAPPING_STATUS_KEYS = {
    "success_statuses",
    "failure_statuses",
}
_ASYNC_MAPPING_KEYS = _ASYNC_MAPPING_POINTER_KEYS | _ASYNC_MAPPING_STATUS_KEYS
_ASYNC_MAPPING_MAX_BYTES = 16 * 1024
_ASYNC_MAPPING_MAX_ITEMS = 16
_ASYNC_POINTER_MAX_BYTES = 512
_ASYNC_POINTER_MAX_DEPTH = 32


@dataclass(frozen=True)
class AsyncResponseMapping:
    task_id_paths: tuple[str, ...] | None = None
    status_paths: tuple[str, ...] | None = None
    result_paths: tuple[str, ...] | None = None
    image_items_paths: tuple[str, ...] | None = None
    image_b64_paths: tuple[str, ...] | None = None
    image_url_paths: tuple[str, ...] | None = None
    revised_prompt_paths: tuple[str, ...] | None = None
    error_message_paths: tuple[str, ...] | None = None
    error_type_paths: tuple[str, ...] | None = None
    error_code_paths: tuple[str, ...] | None = None
    success_statuses: frozenset[str] | None = None
    failure_statuses: frozenset[str] | None = None

    @property
    def configured_keys(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in sorted(_ASYNC_MAPPING_KEYS)
            if getattr(self, name) is not None
        )


def normalize_base_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("base_url cannot be empty.")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute http:// or https:// URL.")
    try:
        parsed.port
    except ValueError:
        raise ValueError("base_url contains an invalid port.") from None
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain user information.")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query string or fragment.")

    path = "/" + parsed.path.strip("/") if parsed.path.strip("/") else ""
    while path.lower().endswith("/v1/v1"):
        path = path[:-3]
    if not path:
        path = "/v1"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path.rstrip("/"), "", ""))


def endpoint_url(base_url: str, endpoint: str) -> str:
    return f"{normalize_base_url(base_url)}/{endpoint.strip('/')}"


def _url_origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("URL contains an invalid port.") from None
    if not parsed.hostname:
        raise ValueError("URL must contain a hostname.")
    scheme = parsed.scheme.lower()
    effective_port = port or (443 if scheme == "https" else 80)
    return scheme, parsed.hostname.lower(), effective_port


def normalize_endpoint_override(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "\\" in raw or any(ord(character) < 32 for character in raw):
        raise ValueError("endpoint override contains invalid characters.")

    parsed = urlsplit(raw)
    if parsed.fragment:
        raise ValueError("endpoint override must not contain a fragment.")
    for name, _value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_name = name.lower().replace("-", "_")
        if (
            normalized_name in _SENSITIVE_ENDPOINT_QUERY_NAMES
            or normalized_name.endswith(
                ("_key", "_token", "_secret", "_password", "_signature")
            )
        ):
            raise ValueError(
                "endpoint override must not place credentials in its query string."
            )
    if parsed.scheme or parsed.netloc:
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "absolute endpoint override must be an http:// or https:// URL."
            )
        try:
            parsed.port
        except ValueError:
            raise ValueError("endpoint override contains an invalid port.") from None
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("endpoint override must not contain user information.")
        if not parsed.path.strip("/"):
            raise ValueError("endpoint override must include an endpoint path.")
        if any(
            unquote(segment) in {".", ".."}
            for segment in parsed.path.split("/")
            if segment
        ):
            raise ValueError("endpoint override must not contain dot path segments.")
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc,
                parsed.path,
                parsed.query,
                "",
            )
        )

    path = parsed.path.lstrip("/")
    if not path:
        raise ValueError("endpoint override must include an endpoint path.")
    if any(
        unquote(segment) in {".", ".."}
        for segment in path.split("/")
        if segment
    ):
        raise ValueError("endpoint override must not contain dot path segments.")
    return path + (f"?{parsed.query}" if parsed.query else "")


def normalize_async_poll_template(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if (
        raw.count("{task_id}") != 1
        or raw.count("{") != 1
        or raw.count("}") != 1
    ):
        raise ValueError(
            "async poll endpoint template must contain exactly one {task_id} placeholder."
        )
    normalized = normalize_endpoint_override(raw)
    parsed = urlsplit(normalized)
    placeholder_in_path = "{task_id}" in parsed.path
    placeholder_in_query = "{task_id}" in parsed.query
    if placeholder_in_path == placeholder_in_query:
        raise ValueError(
            "async poll endpoint template must place {task_id} in either its path "
            "or one query value."
        )
    if placeholder_in_query:
        for pair in parsed.query.split("&"):
            name, separator, item = pair.partition("=")
            if "{task_id}" in name or (separator and item.count("{task_id}") > 1):
                raise ValueError(
                    "async poll endpoint template must place {task_id} in a query "
                    "value, not a query name."
                )
    return normalized


def _validate_json_pointer(value: Any, *, key: str, index: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"async mapping {key}[{index}] must be a string.")
    if len(value.encode("utf-8")) > _ASYNC_POINTER_MAX_BYTES:
        raise ValueError(f"async mapping {key}[{index}] is too long.")
    if value and not value.startswith("/"):
        raise ValueError(
            f"async mapping {key}[{index}] must be an RFC 6901 JSON Pointer."
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"async mapping {key}[{index}] contains control characters.")
    segments = value.split("/")[1:] if value else []
    if len(segments) > _ASYNC_POINTER_MAX_DEPTH:
        raise ValueError(f"async mapping {key}[{index}] is too deep.")
    for segment in segments:
        offset = 0
        while offset < len(segment):
            if segment[offset] != "~":
                offset += 1
                continue
            if offset + 1 >= len(segment) or segment[offset + 1] not in {"0", "1"}:
                raise ValueError(
                    f"async mapping {key}[{index}] contains an invalid JSON Pointer escape."
                )
            offset += 2
    return value


def _normalize_status_name(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _parse_async_mapping_list(
    payload: dict[str, Any],
    *,
    key: str,
    pointer_values: bool,
) -> tuple[str, ...] | frozenset[str] | None:
    if key not in payload:
        return None
    values = payload[key]
    if (
        not isinstance(values, list)
        or not values
        or len(values) > _ASYNC_MAPPING_MAX_ITEMS
    ):
        raise ValueError(
            f"async mapping {key} must be a non-empty array with at most "
            f"{_ASYNC_MAPPING_MAX_ITEMS} items."
        )
    if pointer_values:
        return tuple(
            _validate_json_pointer(value, key=key, index=index)
            for index, value in enumerate(values)
        )

    normalized: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise ValueError(f"async mapping {key}[{index}] must be a string.")
        if (
            not value.strip()
            or len(value.encode("utf-8")) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(f"async mapping {key}[{index}] is invalid.")
        normalized.append(_normalize_status_name(value))
    return frozenset(normalized)


def parse_async_mapping_json(value: str) -> AsyncResponseMapping | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if len(raw.encode("utf-8")) > _ASYNC_MAPPING_MAX_BYTES:
        raise ValueError("async mapping JSON exceeds 16 KiB.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("async mapping must be valid JSON.") from None
    if not isinstance(payload, dict):
        raise ValueError("async mapping must be a JSON object.")
    if not payload:
        return None
    unknown = sorted(set(payload) - _ASYNC_MAPPING_KEYS)
    if unknown:
        raise ValueError(
            "async mapping contains unsupported keys; use only the documented "
            "mapping fields."
        )

    values: dict[str, Any] = {}
    for key in sorted(_ASYNC_MAPPING_POINTER_KEYS):
        values[key] = _parse_async_mapping_list(
            payload,
            key=key,
            pointer_values=True,
        )
    for key in sorted(_ASYNC_MAPPING_STATUS_KEYS):
        values[key] = _parse_async_mapping_list(
            payload,
            key=key,
            pointer_values=False,
        )
    success = values["success_statuses"]
    failure = values["failure_statuses"]
    if success is not None and failure is not None and success.intersection(failure):
        raise ValueError(
            "async mapping success_statuses and failure_statuses must not overlap."
        )
    return AsyncResponseMapping(**values)


def configured_endpoint_url(
    base_url: str,
    endpoint_override: str,
    default_endpoint: str,
) -> str:
    normalized_base = normalize_base_url(base_url)
    configured = normalize_endpoint_override(endpoint_override)
    if not configured:
        return endpoint_url(normalized_base, default_endpoint)

    parsed_override = urlsplit(configured)
    if parsed_override.scheme:
        if _url_origin(configured) != _url_origin(normalized_base):
            raise ValueError(
                "absolute endpoint override must use the same origin as base_url. "
                "Change base_url when targeting another host."
            )
        return configured

    endpoint = parsed_override.path
    base_path = urlsplit(normalized_base).path.rstrip("/").lower()
    if base_path.endswith("/v1") and endpoint.lower().startswith("v1/"):
        endpoint = endpoint[3:]
    result = f"{normalized_base}/{endpoint}"
    return result + (f"?{parsed_override.query}" if parsed_override.query else "")


@dataclass(frozen=True)
class ProviderConfig:
    auth_mode: str
    base_url: str
    model: str
    api_protocol: str
    api_key: str = field(default="", repr=False)
    use_custom_endpoints: bool = False
    use_async: bool = True
    generate_endpoint: str = ""
    edit_endpoint: str = ""
    async_generate_endpoint: str = ""
    async_edit_endpoint: str = ""
    async_poll_endpoint_template: str = ""
    async_mapping_json: str = field(default="", repr=False)
    async_mapping: AsyncResponseMapping | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.auth_mode not in AUTH_MODES:
            raise ValueError(f"Unsupported auth_mode: {self.auth_mode}")
        allowed_protocols = (
            API_PROTOCOLS if self.auth_mode == AUTH_MODE_API else OAUTH_PROTOCOLS
        )
        if self.api_protocol not in allowed_protocols:
            raise ValueError(f"Unsupported api_protocol: {self.api_protocol}")
        if not self.model.strip():
            raise ValueError("model cannot be empty.")
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "api_key", self.api_key.strip())
        if self.api_key and any(
            ord(character) < 0x21 or ord(character) > 0x7E
            for character in self.api_key
        ):
            raise ValueError(
                "api_key must contain only visible ASCII characters without spaces."
            )
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))
        object.__setattr__(
            self,
            "use_custom_endpoints",
            bool(self.use_custom_endpoints),
        )
        object.__setattr__(self, "use_async", bool(self.use_async))
        if self.auth_mode == AUTH_MODE_OAUTH:
            if (
                self.generate_endpoint
                or self.edit_endpoint
                or self.async_generate_endpoint
                or self.async_edit_endpoint
                or self.async_poll_endpoint_template
                or self.async_mapping_json
            ):
                raise ValueError(
                    "Codex OAuth does not accept custom endpoint overrides."
                )
            object.__setattr__(self, "use_custom_endpoints", False)
            object.__setattr__(self, "use_async", False)
            object.__setattr__(self, "generate_endpoint", "")
            object.__setattr__(self, "edit_endpoint", "")
            object.__setattr__(self, "async_generate_endpoint", "")
            object.__setattr__(self, "async_edit_endpoint", "")
            object.__setattr__(self, "async_poll_endpoint_template", "")
            object.__setattr__(self, "async_mapping_json", "")
            object.__setattr__(self, "async_mapping", None)
            return

        if not self.use_custom_endpoints:
            # Keep disabled endpoint text in the local workflow widgets, not in
            # the executable provider object. This also means stale or malformed
            # disabled endpoint text cannot affect routing.
            object.__setattr__(self, "generate_endpoint", "")
            object.__setattr__(self, "edit_endpoint", "")
            object.__setattr__(self, "async_generate_endpoint", "")
            object.__setattr__(self, "async_edit_endpoint", "")
            object.__setattr__(self, "async_poll_endpoint_template", "")
            object.__setattr__(self, "async_mapping_json", "")
            object.__setattr__(self, "async_mapping", None)
            return
        object.__setattr__(
            self,
            "generate_endpoint",
            normalize_endpoint_override(self.generate_endpoint),
        )
        object.__setattr__(
            self,
            "edit_endpoint",
            normalize_endpoint_override(self.edit_endpoint),
        )
        if not self.use_async:
            object.__setattr__(self, "async_generate_endpoint", "")
            object.__setattr__(self, "async_edit_endpoint", "")
            object.__setattr__(self, "async_poll_endpoint_template", "")
            object.__setattr__(self, "async_mapping_json", "")
            object.__setattr__(self, "async_mapping", None)
            return
        object.__setattr__(
            self,
            "async_generate_endpoint",
            normalize_endpoint_override(self.async_generate_endpoint),
        )
        object.__setattr__(
            self,
            "async_edit_endpoint",
            normalize_endpoint_override(self.async_edit_endpoint),
        )
        object.__setattr__(
            self,
            "async_poll_endpoint_template",
            normalize_async_poll_template(self.async_poll_endpoint_template),
        )
        mapping_json = (self.async_mapping_json or "").strip()
        object.__setattr__(self, "async_mapping_json", mapping_json)
        object.__setattr__(
            self,
            "async_mapping",
            parse_async_mapping_json(mapping_json),
        )

    @classmethod
    def from_api_inputs(
        cls,
        *,
        api_key: str,
        base_url: str,
        model: str,
        api_protocol: str,
        generate_endpoint: str = "",
        edit_endpoint: str = "",
        use_custom_endpoints: bool | None = None,
        use_async: bool = True,
        async_generate_endpoint: str = "",
        async_edit_endpoint: str = "",
        async_poll_endpoint_template: str = "",
        async_mapping_json: str = "",
        require_session_handle: bool = False,
    ) -> "ProviderConfig":
        if use_custom_endpoints is None:
            # Compatibility for Python callers written before the switch
            # existed. The ComfyUI node always supplies an explicit Boolean.
            use_custom_endpoints = bool(
                str(generate_endpoint or "").strip()
                or str(edit_endpoint or "").strip()
                or str(async_generate_endpoint or "").strip()
                or str(async_edit_endpoint or "").strip()
                or str(async_poll_endpoint_template or "").strip()
                or str(async_mapping_json or "").strip()
            )
        return cls(
            auth_mode=AUTH_MODE_API,
            api_key=consume_session_credential(
                api_key,
                require_handle=require_session_handle,
            ),
            base_url=base_url,
            model=model,
            api_protocol=api_protocol,
            use_custom_endpoints=use_custom_endpoints,
            use_async=use_async,
            generate_endpoint=generate_endpoint,
            edit_endpoint=edit_endpoint,
            async_generate_endpoint=async_generate_endpoint,
            async_edit_endpoint=async_edit_endpoint,
            async_poll_endpoint_template=async_poll_endpoint_template,
            async_mapping_json=async_mapping_json,
        )

    @classmethod
    def from_oauth_inputs(
        cls,
        *,
        model: str,
        api_protocol: str,
    ) -> "ProviderConfig":
        return cls(
            auth_mode=AUTH_MODE_OAUTH,
            base_url=DEFAULT_API_BASE_URL,
            model=model,
            api_protocol=api_protocol,
            use_custom_endpoints=False,
            use_async=False,
        )

    def public_dict(self) -> dict[str, Any]:
        """Return diagnostic fields without ever serializing the API key."""
        result: dict[str, Any] = {
            "auth_mode": self.auth_mode,
            "base_url": self.base_url,
            "model": self.model,
            "api_protocol": self.api_protocol,
        }
        if self.auth_mode == AUTH_MODE_API:
            result["api_key_configured"] = bool(self.api_key)
            result["use_custom_endpoints"] = bool(self.use_custom_endpoints)
            result["use_async"] = bool(self.use_async)
            result["generate_endpoint"] = self.generate_endpoint
            result["edit_endpoint"] = self.edit_endpoint
            result["async_generate_endpoint"] = self.async_generate_endpoint
            result["async_edit_endpoint"] = self.async_edit_endpoint
            result["async_poll_endpoint_template"] = self.async_poll_endpoint_template
            result["async_mapping"] = {
                "configured": self.async_mapping is not None,
                "keys": (
                    list(self.async_mapping.configured_keys)
                    if self.async_mapping is not None
                    else []
                ),
            }
        return result


def resolve_api_credential(provider: ProviderConfig) -> str:
    if provider.auth_mode != AUTH_MODE_API:
        raise BridgeError(
            "API key resolution was requested for a Codex OAuth provider.",
            error_code="auth_mode_mismatch",
        )
    if provider.api_key:
        return provider.api_key
    raise BridgeError(
        "API key is empty. Enter it in GPT Image Bridge · API Provider.",
        error_type="authentication",
        error_code="credential_missing",
    )


def with_protocol(provider: ProviderConfig, protocol: str) -> ProviderConfig:
    return replace(provider, api_protocol=protocol)
