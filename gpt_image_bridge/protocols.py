from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from .config import (
    AsyncResponseMapping,
    ProviderConfig,
    configured_endpoint_url,
    resolve_api_credential,
)
from .errors import BridgeError, redact_text, sanitize
from .images import (
    EncodedImage,
    b64_image_to_bytes,
    renumber_references,
    validate_image_bytes,
)
from .oauth import oauth_request_context
from .transport import HttpClient, HttpResponse


CLIENT_HEADER = "comfyui-gpt-image-bridge/0.5.2"
ASYNC_POLL_INTERVAL_SECONDS = 3.0
ASYNC_TASK_ID_MAX_LENGTH = 512
_ASYNC_ERROR_MESSAGE_MAX_CHARS = 2048
_ASYNC_ERROR_FIELD_MAX_CHARS = 256
_ASYNC_UNSUPPORTED_STATUSES = {404, 405, 501}
_ASYNC_PENDING_STATUSES = {
    "",
    "created",
    "queued",
    "pending",
    "running",
    "processing",
    "in_progress",
}
_ASYNC_SUCCESS_STATUSES = {"success", "succeeded", "completed"}
_ASYNC_FAILURE_STATUSES = {
    "failed",
    "failure",
    "error",
    "cancelled",
    "canceled",
    "expired",
}
GENERATE_DEVELOPER_PROMPT = (
    "Generate a new image rather than editing any reference. Attached reference images, "
    "when present, are context only and are named Reference 1, Reference 2, and so on "
    "in the exact order supplied by the user. Return image output, not an explanation."
)
EDIT_DEVELOPER_PROMPT = (
    "Edit Image 1, the base image. Images 2 onward are references only. The returned image "
    "must be an edit of Image 1, never an unchanged reference image. Preserve areas the user "
    "did not ask to change. Return image output, not an explanation."
)


@dataclass(frozen=True)
class OperationResult:
    images: list[bytes]
    revised_prompt: str
    report: dict[str, Any]


def _prompt_descriptor(prompt: str) -> dict[str, Any]:
    encoded = prompt.encode("utf-8")
    return {
        "type": "input_text",
        "characters": len(prompt),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _protocol_from_endpoint_override(
    provider: ProviderConfig,
    *,
    operation: str,
) -> str | None:
    override = (
        provider.generate_endpoint if operation == "generate" else provider.edit_endpoint
    )
    if not override:
        return None
    path = urlsplit(override).path.rstrip("/").lower()
    if path == "responses" or path.endswith("/responses"):
        return "responses"
    if path == "chat/completions" or path.endswith("/chat/completions"):
        return "chat_completions"
    images_path = path.removesuffix("/async")
    if (
        images_path in {"images/generations", "images/edits"}
        or images_path.endswith("/images/generations")
        or images_path.endswith("/images/edits")
    ):
        expected_operation = (
            "generate" if images_path.endswith("/generations") else "edit"
        )
        if expected_operation != operation:
            raise BridgeError(
                f"The configured {operation} endpoint points to an Images "
                f"{expected_operation} route.",
                error_code="endpoint_operation_mismatch",
                operation=operation,
                endpoint=override,
            )
        return "images"
    return None


def _custom_async_requested(
    provider: ProviderConfig,
    *,
    operation: str,
) -> bool:
    operation_endpoint = (
        provider.async_generate_endpoint
        if operation == "generate"
        else provider.async_edit_endpoint
    )
    return bool(
        provider.use_custom_endpoints
        and provider.use_async
        and (
            operation_endpoint
            or provider.async_poll_endpoint_template
            or provider.async_mapping is not None
        )
    )


def _validate_protocol_inputs(
    protocol: str,
    *,
    operation: str,
    has_references: bool,
    has_mask: bool,
) -> None:
    if protocol == "images" and operation == "generate" and has_references:
        raise BridgeError(
            "The Images generations protocol has no reference-image role. "
            "Choose responses or chat_completions; no semantic fallback was attempted.",
            error_code="images_generate_references_unsupported",
            operation=operation,
            protocol=protocol,
        )
    if protocol == "responses" and has_mask:
        raise BridgeError(
            "Mask editing is only represented by the Images edits protocol. "
            "Choose images; no semantic fallback was attempted.",
            error_code="responses_mask_unsupported",
            operation=operation,
            protocol=protocol,
        )
    if protocol == "chat_completions" and has_mask:
        raise BridgeError(
            "Chat Completions has no portable mask field. "
            "Choose images or remove the mask; no semantic fallback was attempted.",
            error_code="chat_completions_mask_unsupported",
            operation=operation,
            protocol=protocol,
        )


def select_protocol(
    provider: ProviderConfig,
    *,
    operation: str,
    has_references: bool,
    has_mask: bool = False,
) -> tuple[str, str]:
    requested = provider.api_protocol
    endpoint_protocol = _protocol_from_endpoint_override(
        provider,
        operation=operation,
    )
    if requested != "auto":
        if _custom_async_requested(provider, operation=operation) and requested != "images":
            raise BridgeError(
                "Custom async endpoints and mappings apply only to the Images protocol.",
                error_code="custom_async_requires_images",
                operation=operation,
                protocol=requested,
            )
        if endpoint_protocol is not None and endpoint_protocol != requested:
            raise BridgeError(
                f"The configured endpoint identifies {endpoint_protocol!r}, but "
                f"api_protocol is {requested!r}.",
                error_code="endpoint_protocol_mismatch",
                operation=operation,
                protocol=requested,
                endpoint=(
                    provider.generate_endpoint
                    if operation == "generate"
                    else provider.edit_endpoint
                ),
            )
        _validate_protocol_inputs(
            requested,
            operation=operation,
            has_references=has_references,
            has_mask=has_mask,
        )
        return requested, f"explicit provider protocol {requested!r}"

    if endpoint_protocol is not None:
        _validate_protocol_inputs(
            endpoint_protocol,
            operation=operation,
            has_references=has_references,
            has_mask=has_mask,
        )
        return (
            endpoint_protocol,
            f"auto: explicit {operation} endpoint identifies {endpoint_protocol!r}",
        )

    if _custom_async_requested(provider, operation=operation):
        _validate_protocol_inputs(
            "images",
            operation=operation,
            has_references=has_references,
            has_mask=has_mask,
        )
        return "images", "auto: custom async configuration requires Images"

    if provider.auth_mode == "codex_oauth":
        if operation == "edit":
            return "images", "auto: native Codex OAuth edit route uses /images/edits"
        return "responses", "auto: native Codex OAuth generation route uses /responses"
    if operation == "edit":
        return "images", "auto: explicit base-image semantics use /images/edits"
    if has_references:
        return "responses", "auto: generation references require ordered input_image roles"
    return "images", "auto: plain text-to-image uses /images/generations"


def _auth_context(
    provider: ProviderConfig,
    *,
    timeout: int,
) -> tuple[str, dict[str, str], tuple[str, ...]]:
    if provider.auth_mode == "codex_oauth":
        return oauth_request_context(timeout=min(max(int(timeout), 1), 30))
    secret = resolve_api_credential(provider)
    return provider.base_url, {"Authorization": f"Bearer {secret}"}, (secret,)


def _headers(auth_headers: dict[str, str], *, content_type: str, accept: str) -> dict[str, str]:
    return {
        "Accept": accept,
        "Content-Type": content_type,
        "User-Agent": CLIENT_HEADER,
        "X-GPT-Image-Bridge-Client": "comfyui",
        **auth_headers,
    }


def _json_response(response: HttpResponse, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BridgeError(
            f"{label} returned invalid JSON.",
            status=response.status,
            request_id=response.request_id,
            error_code="invalid_json",
            endpoint=response.url,
            attempts=response.attempts,
        ) from None
    if not isinstance(payload, dict):
        raise BridgeError(
            f"{label} returned a JSON value that is not an object.",
            status=response.status,
            request_id=response.request_id,
            error_code="invalid_json_shape",
            endpoint=response.url,
            attempts=response.attempts,
        )
    return payload


def _extract_response_output(payload: dict[str, Any]) -> tuple[list[str], str]:
    images: list[str] = []
    revised_prompt = ""
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "image_generation_call":
            result = item.get("result")
            if isinstance(result, str) and result:
                images.append(result)
            elif isinstance(result, list):
                images.extend(value for value in result if isinstance(value, str) and value)
            revised_prompt = item.get("revised_prompt") or revised_prompt
    if images:
        return images, revised_prompt

    refusal_parts: list[str] = []
    text_parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "refusal" and content.get("refusal"):
                refusal_parts.append(str(content["refusal"]))
            if content.get("type") == "output_text" and content.get("text"):
                text_parts.append(str(content["text"]))
    if refusal_parts:
        raise BridgeError(
            "Provider refused the image request: " + " ".join(refusal_parts),
            error_type="safety_refusal",
            error_code="image_request_refused",
            moderation_category=payload.get("moderation_category"),
            retryable=False,
        )
    detail = " ".join(text_parts).strip()
    raise BridgeError(
        "No image data was returned." + (f" Provider text: {detail}" if detail else ""),
        error_code="image_output_missing",
        retryable=False,
    )


def _parse_sse(body: bytes) -> tuple[list[str], str]:
    text = body.decode("utf-8", errors="replace").replace("\r\n", "\n")
    final_response: dict[str, Any] | None = None
    output_items: dict[str, dict[str, Any]] = {}
    unkeyed_done_items: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        block_lines = block.splitlines()
        event_names = [
            line[6:].strip()
            for line in block_lines
            if line.startswith("event:")
        ]
        sse_event_type = event_names[-1] if event_names else ""
        data_lines = [
            line[5:].lstrip()
            for line in block_lines
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        data_text = "\n".join(data_lines)
        if data_text == "[DONE]":
            continue
        try:
            event = json.loads(data_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        payload_event_type = event.get("type")
        event_type = (
            payload_event_type
            if isinstance(payload_event_type, str) and payload_event_type
            else sse_event_type
        )
        event_types = {
            value
            for value in (
                payload_event_type
                if isinstance(payload_event_type, str)
                else "",
                sse_event_type,
            )
            if value
        }
        if "error" in event_types:
            raw_error = event.get("error")
            error = raw_error if isinstance(raw_error, dict) else event
            raise BridgeError(
                error.get("message") or "Response stream reported an error.",
                error_type=error.get("type"),
                error_code=error.get("code"),
                moderation_category=error.get("moderation_category") or error.get("category"),
            )
        raw_response = event.get("response")
        response = raw_response if isinstance(raw_response, dict) else {}
        response_status = response.get("status")
        terminal_event_types = event_types.intersection({
            "response.failed",
            "response.incomplete",
            "response.cancelled",
            "response.canceled",
        })
        if (
            isinstance(response_status, str)
            and response_status
            in {"failed", "incomplete", "cancelled", "canceled"}
        ):
            terminal_event_types.add(f"response.{response_status}")
        if terminal_event_types:
            raw_error = response.get("error") or event.get("error")
            error = raw_error if isinstance(raw_error, dict) else {}
            raw_details = response.get("incomplete_details")
            details = raw_details if isinstance(raw_details, dict) else {}
            message = error.get("message") or details.get("reason")
            if not message and isinstance(raw_error, str):
                message = raw_error
            terminal_event_type = sorted(terminal_event_types)[0]
            raise BridgeError(
                message or terminal_event_type,
                error_type=error.get("type") or terminal_event_type,
                error_code=error.get("code"),
                moderation_category=error.get("moderation_category") or error.get("category"),
            )
        raw_item = event.get("item")
        if isinstance(raw_item, dict):
            item_id = raw_item.get("id")
            if isinstance(item_id, str) and item_id:
                output_items[item_id] = raw_item
            elif event_type == "response.output_item.done":
                unkeyed_done_items.append(raw_item)
        if "response.completed" in event_types:
            final_response = response

    collected_items = [*output_items.values(), *unkeyed_done_items]
    if final_response is not None:
        if not final_response.get("output") and collected_items:
            final_response = {**final_response, "output": collected_items}
        return _extract_response_output(final_response)
    if collected_items:
        return _extract_response_output({"output": collected_items})
    raise BridgeError(
        "Response stream ended without a completed image.",
        error_code="incomplete_image_stream",
        retryable=False,
    )


def _redact_bridge_error(error: BridgeError, known_secrets: tuple[str, ...]) -> None:
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


def _parse_responses(
    response: HttpResponse,
    *,
    known_secrets: tuple[str, ...] = (),
) -> tuple[list[bytes], str, list[dict[str, Any]]]:
    try:
        content_type = response.headers.get("content-type", "")
        body_start = response.body.lstrip()
        if (
            "text/event-stream" in content_type.lower()
            or body_start.startswith(b"data:")
            or body_start.startswith(b"event:")
        ):
            values, revised_prompt = _parse_sse(response.body)
        else:
            values, revised_prompt = _extract_response_output(
                _json_response(response, "Responses API")
            )
        decoded = [b64_image_to_bytes(value) for value in values]
        return (
            [item[0] for item in decoded],
            redact_text(revised_prompt, known_secrets),
            [item[1] for item in decoded],
        )
    except BridgeError as error:
        _annotate_parse_error(error, response, protocol="responses")
        _redact_bridge_error(error, known_secrets)
        raise
    except ValueError as error:
        raise BridgeError(
            redact_text(error, known_secrets),
            status=response.status,
            request_id=response.request_id,
            error_code="invalid_image_data",
            protocol="responses",
            endpoint=response.url,
            attempts=response.attempts,
        ) from None


def _annotate_parse_error(
    error: BridgeError,
    response: HttpResponse,
    *,
    protocol: str,
) -> None:
    if error.status is None:
        error.status = response.status
    if error.request_id is None:
        error.request_id = response.request_id
    if error.protocol is None:
        error.protocol = protocol
    if error.endpoint is None:
        error.endpoint = response.url
    error.attempts = max(error.attempts, response.attempts)


def _download_image(
    client: HttpClient,
    *,
    url: str,
    timeout: int,
    known_secrets: tuple[str, ...] = (),
    deadline: float | None = None,
) -> tuple[bytes, dict[str, Any]]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BridgeError(
            "Provider returned an invalid image URL.",
            error_code="invalid_image_url",
        )
    effective_timeout = int(timeout)
    max_attempts = 3
    if deadline is not None:
        remaining = deadline - client.monotonic()
        if remaining <= 0:
            raise BridgeError(
                "Async image result download exceeded timeout_sec.",
                error_code="async_result_timeout",
                protocol="images",
                phase="async_result",
                retryable=False,
                safe_to_resubmit=False,
                remote_task_may_continue=False,
            )
        effective_timeout = max(1, math.ceil(remaining))
        # A single attempt keeps the async operation inside its shared
        # deadline. Retrying a result download is safe, but giving every retry
        # a fresh timeout would silently exceed the user's upper bound.
        max_attempts = 1
    response = client.request(
        url=url,
        method="GET",
        headers={"Accept": "image/*", "User-Agent": CLIENT_HEADER},
        timeout=effective_timeout,
        max_attempts=max_attempts,
        operation="download_output",
        protocol="url",
        known_secrets=known_secrets,
        max_response_bytes=50 * 1024 * 1024,
        public_url_only=True,
    )
    try:
        return validate_image_bytes(
            response.body,
            content_type=response.headers.get("content-type", ""),
        )
    except ValueError as error:
        raise BridgeError(
            str(error),
            status=response.status,
            request_id=response.request_id,
            error_code="invalid_downloaded_image",
            endpoint=response.url,
            attempts=response.attempts,
        ) from None


def _parse_images(
    response: HttpResponse,
    *,
    client: HttpClient,
    timeout: int,
    known_secrets: tuple[str, ...] = (),
    deadline: float | None = None,
) -> tuple[list[bytes], str, list[dict[str, Any]]]:
    try:
        payload = _json_response(response, "Images API")
        entries = payload.get("data") or []
        if not isinstance(entries, list) or not entries:
            raise BridgeError(
                "Images API returned no image entries.",
                error_code="image_output_missing",
            )
        images: list[bytes] = []
        metadata: list[dict[str, Any]] = []
        revised: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("b64_json"), str):
                image, info = b64_image_to_bytes(entry["b64_json"])
            elif isinstance(entry.get("url"), str):
                image, info = _download_image(
                    client,
                    url=entry["url"],
                    timeout=timeout,
                    known_secrets=known_secrets,
                    deadline=deadline,
                )
            else:
                continue
            images.append(image)
            metadata.append(info)
            if entry.get("revised_prompt"):
                revised.append(str(entry["revised_prompt"]))
        if not images:
            raise BridgeError(
                "Images API entries contained neither b64_json nor URL image data.",
                error_code="image_data_missing",
            )
        return images, redact_text(next(iter(revised), ""), known_secrets), metadata
    except BridgeError as error:
        _annotate_parse_error(error, response, protocol="images")
        _redact_bridge_error(error, known_secrets)
        raise
    except ValueError as error:
        raise BridgeError(
            redact_text(error, known_secrets),
            status=response.status,
            request_id=response.request_id,
            error_code="invalid_image_data",
            protocol="images",
            endpoint=response.url,
            attempts=response.attempts,
        ) from None


_DATA_IMAGE_RE = re.compile(
    r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)
_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\((https?://[^)\s]+)\)",
    re.IGNORECASE,
)
_ABSOLUTE_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_RAW_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{256,}={0,2}")


def _chat_text_candidates(text: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = [
        ("base64", match.group(0))
        for match in _DATA_IMAGE_RE.finditer(text)
    ]
    candidates.extend(
        ("url", match.group(1))
        for match in _MARKDOWN_IMAGE_RE.finditer(text)
    )
    if candidates:
        return candidates

    stripped = text.strip()
    if _ABSOLUTE_URL_RE.fullmatch(stripped):
        return [("url", stripped)]
    if _RAW_BASE64_RE.fullmatch(stripped):
        return [("base64", stripped)]
    return []


def _chat_structured_candidates(value: Any) -> list[tuple[str, str]]:
    if isinstance(value, str):
        return _chat_text_candidates(value)
    if isinstance(value, list):
        result: list[tuple[str, str]] = []
        for item in value:
            result.extend(_chat_structured_candidates(item))
        return result
    if not isinstance(value, dict):
        return []

    result: list[tuple[str, str]] = []
    for key in ("b64_json", "base64", "b64"):
        item = value.get(key)
        if isinstance(item, str) and item:
            result.append(("base64", item))
    data = value.get("data")
    if isinstance(data, str) and data:
        result.append(
            ("base64", data)
            if data.startswith("data:image/") or _RAW_BASE64_RE.fullmatch(data)
            else ("url", data)
        )
    for key in ("url", "image_url"):
        item = value.get(key)
        if isinstance(item, str) and item:
            result.append(
                ("base64", item) if item.startswith("data:image/") else ("url", item)
            )
        elif isinstance(item, dict):
            result.extend(_chat_structured_candidates(item))
    source = value.get("source")
    if isinstance(source, dict):
        result.extend(_chat_structured_candidates(source))
    return result


def _extract_chat_output(
    payload: dict[str, Any],
) -> tuple[list[tuple[str, str]], str, list[str]]:
    error = payload.get("error")
    if isinstance(error, dict):
        raise BridgeError(
            error.get("message") or "Chat Completions returned an error.",
            error_type=error.get("type"),
            error_code=error.get("code"),
            moderation_category=(
                error.get("moderation_category") or error.get("category")
            ),
            retryable=False,
        )

    candidates: list[tuple[str, str]] = []
    revised_prompt = ""
    diagnostic_text: list[str] = []
    choices = payload.get("choices") or []
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            finish_reason = str(choice.get("finish_reason") or "").lower()
            message = choice.get("message") or choice.get("delta") or {}
            if not isinstance(message, dict):
                continue
            refusal = message.get("refusal")
            if refusal or finish_reason in {"content_filter", "safety"}:
                raise BridgeError(
                    "Provider refused the image request."
                    + (f" {refusal}" if refusal else ""),
                    error_type="safety_refusal",
                    error_code="image_request_refused",
                    moderation_category=(
                        payload.get("moderation_category") or finish_reason or None
                    ),
                    retryable=False,
                )

            for key in ("images", "image", "attachments"):
                candidates.extend(_chat_structured_candidates(message.get(key)))
            content = message.get("content")
            if isinstance(content, str):
                candidates.extend(_chat_text_candidates(content))
                if content.strip():
                    diagnostic_text.append(content.strip())
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    part_type = str(part.get("type") or "").lower()
                    if part_type in {
                        "image",
                        "image_url",
                        "input_image",
                        "output_image",
                    } or any(
                        key in part
                        for key in ("image_url", "url", "b64_json", "base64", "b64")
                    ):
                        candidates.extend(_chat_structured_candidates(part))
                    elif part_type in {"text", "output_text"} and part.get("text"):
                        text = str(part["text"])
                        candidates.extend(_chat_text_candidates(text))
                        diagnostic_text.append(text)
                    elif part_type == "refusal" and part.get("refusal"):
                        raise BridgeError(
                            "Provider refused the image request. "
                            + str(part["refusal"]),
                            error_type="safety_refusal",
                            error_code="image_request_refused",
                            moderation_category=payload.get("moderation_category"),
                            retryable=False,
                        )
            revised_prompt = str(
                message.get("revised_prompt")
                or choice.get("revised_prompt")
                or revised_prompt
            )

    data = payload.get("data")
    if isinstance(data, list):
        candidates.extend(_chat_structured_candidates(data))

    unique: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if candidate not in seen:
            unique.append(candidate)
            seen.add(candidate)
    return unique, revised_prompt, diagnostic_text


def _parse_chat_completions(
    response: HttpResponse,
    *,
    client: HttpClient,
    timeout: int,
    known_secrets: tuple[str, ...] = (),
) -> tuple[list[bytes], str, list[dict[str, Any]]]:
    try:
        payload = _json_response(response, "Chat Completions API")
        candidates, revised_prompt, diagnostic_text = _extract_chat_output(payload)
        if not candidates:
            detail = redact_text(" ".join(diagnostic_text), known_secrets).strip()
            if len(detail) > 500:
                detail = detail[:500] + "…"
            raise BridgeError(
                "Chat Completions returned no image data."
                + (f" Provider text: {detail}" if detail else ""),
                error_code="image_output_missing",
                retryable=False,
            )

        images: list[bytes] = []
        metadata: list[dict[str, Any]] = []
        for kind, value in candidates:
            if kind == "url" and value.lower().startswith(("http://", "https://")):
                image, info = _download_image(
                    client,
                    url=value,
                    timeout=timeout,
                    known_secrets=known_secrets,
                )
            else:
                image, info = b64_image_to_bytes(value)
            images.append(image)
            metadata.append(info)
        return (
            images,
            redact_text(revised_prompt, known_secrets),
            metadata,
        )
    except BridgeError as error:
        _annotate_parse_error(error, response, protocol="chat_completions")
        _redact_bridge_error(error, known_secrets)
        raise
    except ValueError as error:
        raise BridgeError(
            redact_text(error, known_secrets),
            status=response.status,
            request_id=response.request_id,
            error_code="invalid_image_data",
            protocol="chat_completions",
            endpoint=response.url,
            attempts=response.attempts,
        ) from None


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode("utf-8")


def _multipart_file(
    boundary: str,
    field_name: str,
    filename: str,
    payload: bytes,
    content_type: str = "image/png",
) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + payload + b"\r\n"


def _build_edit_multipart(
    *,
    provider: ProviderConfig,
    base_image: EncodedImage,
    references: tuple[EncodedImage, ...],
    mask_png: bytes | None,
    prompt: str,
    size: str,
    quality: str,
    background: str,
    output_format: str,
    n: int,
) -> tuple[bytes, str, str]:
    boundary = f"----ComfyUIGPTImageBridge{uuid.uuid4().hex}"
    image_field = (
        "image"
        if provider.auth_mode == "codex_oauth" or not references
        else "image[]"
    )
    role_prompt = (
        "Image 1 is the base image to edit. "
        + (
            f"Images 2 through {len(references) + 1} are ordered reference images only. "
            if references
            else ""
        )
        + prompt
    )
    parts = [
        _multipart_field(boundary, "model", provider.model),
        _multipart_field(boundary, "prompt", role_prompt),
        _multipart_field(boundary, "size", size),
        _multipart_field(boundary, "quality", quality),
    ]
    if provider.auth_mode == "codex_oauth":
        parts.append(_multipart_field(boundary, "response_format", "b64_json"))
    else:
        parts.extend(
            [
                _multipart_field(boundary, "background", background),
                _multipart_field(boundary, "output_format", output_format),
            ]
        )
    parts.extend(
        [
            _multipart_field(boundary, "n", str(int(n))),
            _multipart_file(
                boundary,
                image_field,
                "image-1-base.png",
                base_image.png_bytes,
            ),
        ]
    )
    for index, reference in enumerate(references, start=2):
        parts.append(
            _multipart_file(
                boundary,
                image_field,
                f"image-{index}-reference.png",
                reference.png_bytes,
            )
        )
    if mask_png is not None:
        parts.append(_multipart_file(boundary, "mask", "mask.png", mask_png))
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}", image_field


def _oauth_edit_json_payload(
    *,
    provider: ProviderConfig,
    base_image: EncodedImage,
    references: tuple[EncodedImage, ...],
    mask_png: bytes | None,
    prompt: str,
    size: str,
    quality: str,
    n: int,
) -> dict[str, Any]:
    if mask_png is not None:
        raise BridgeError(
            "Image masks are not supported by Codex OAuth image editing.",
            error_type="invalid_request_error",
            error_code="oauth_mask_unsupported",
            operation="edit",
            protocol="images",
            retryable=False,
        )
    images = (base_image, *references)
    if len(images) > 5:
        raise BridgeError(
            "Codex OAuth supports at most 5 ordered images total: "
            "Image 1 plus up to 4 references.",
            error_type="invalid_request_error",
            error_code="oauth_reference_limit",
            operation="edit",
            protocol="images",
            retryable=False,
        )
    if any(len(image.png_bytes) > 50 * 1024 * 1024 for image in images):
        raise BridgeError(
            "Each Codex OAuth image must be 50 MiB or smaller after PNG encoding.",
            error_type="invalid_request_error",
            error_code="oauth_image_too_large",
            operation="edit",
            protocol="images",
            retryable=False,
        )
    role_prompt = (
        "Image 1 is the base image to edit. "
        + (
            f"Images 2 through {len(images)} are ordered reference images only. "
            if references
            else ""
        )
        + prompt
    )
    return {
        "images": [{"image_url": image.data_url} for image in images],
        "model": provider.model,
        "prompt": role_prompt,
        "n": int(n),
        "quality": quality,
        "size": size,
    }


def _responses_payload(
    *,
    operation: str,
    provider: ProviderConfig,
    prompt: str,
    base_image: EncodedImage | None,
    references: tuple[EncodedImage, ...],
    size: str,
    quality: str,
    background: str,
    output_format: str,
    moderation: str,
) -> dict[str, Any]:
    images = ([base_image] if base_image is not None else []) + list(references)
    role_text = (
        (
            "Edit Image 1 (the base image). "
            + (
                f"Images 2 through {len(images)} are ordered references only. "
                if references
                else ""
            )
        )
        if operation == "edit"
        else (
            f"Use References 1 through {len(images)} as ordered context only. "
            if images
            else ""
        )
    )
    user_content: Any
    if images:
        user_content = [
            *[
                {"type": "input_image", "image_url": image.data_url}
                for image in images
                if image is not None
            ],
            {"type": "input_text", "text": role_text + prompt},
        ]
    else:
        user_content = role_text + prompt
    payload = {
        "model": provider.model,
        "input": [
            {
                "role": "developer",
                "content": (
                    EDIT_DEVELOPER_PROMPT if operation == "edit" else GENERATE_DEVELOPER_PROMPT
                ),
            },
            {"role": "user", "content": user_content},
        ],
        "tools": [
            {
                "type": "image_generation",
                "action": operation,
                "size": size,
                "quality": quality,
                "background": background,
                "output_format": output_format,
                "moderation": moderation,
            }
        ],
        "tool_choice": {"type": "image_generation"},
        "stream": True,
    }
    if provider.auth_mode == "codex_oauth":
        # The ChatGPT Codex backend always streams Responses and does not
        # persist server-side replay state. Apply those native transport
        # invariants directly to the request.
        payload["instructions"] = ""
        payload["store"] = False
        payload["include"] = ["reasoning.encrypted_content"]
        payload["stream"] = True
        payload.pop("max_output_tokens", None)
    return payload


def _chat_completions_payload(
    *,
    operation: str,
    provider: ProviderConfig,
    prompt: str,
    base_image: EncodedImage | None,
    references: tuple[EncodedImage, ...],
    size: str,
) -> dict[str, Any]:
    images = ([base_image] if base_image is not None else []) + list(references)
    if operation == "edit":
        role_text = (
            "Image 1 is the only base image to edit. "
            + (
                f"Images 2 through {len(images)} are ordered references only; "
                "do not return a reference image unchanged. "
                if references
                else ""
            )
        )
        instructions = EDIT_DEVELOPER_PROMPT
    else:
        role_text = (
            f"References 1 through {len(images)} are ordered context only. "
            if images
            else ""
        )
        instructions = GENERATE_DEVELOPER_PROMPT
    size_text = f" Requested output size: {size}." if size != "auto" else ""
    text = f"{instructions}\n\n{role_text}{prompt}{size_text}"
    content: str | list[dict[str, Any]]
    if images:
        content = [{"type": "text", "text": text}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": image.data_url},
            }
            for image in images
            if image is not None
        )
    else:
        content = text
    return {
        "model": provider.model,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
    }


def _check_comfy_interrupted() -> None:
    try:
        from comfy.model_management import (  # type: ignore[import-not-found]
            throw_exception_if_processing_interrupted,
        )
    except (ImportError, ModuleNotFoundError):
        return
    throw_exception_if_processing_interrupted()


def _images_request(
    *,
    provider: ProviderConfig,
    operation: str,
    base_url: str,
    endpoint_override: str,
    auth_headers: dict[str, str],
    prompt: str,
    base_image: EncodedImage | None,
    references: tuple[EncodedImage, ...],
    mask_png: bytes | None,
    size: str,
    quality: str,
    background: str,
    output_format: str,
    n: int,
) -> tuple[str, bytes, dict[str, str], str | None]:
    if operation == "generate":
        url = configured_endpoint_url(
            base_url,
            endpoint_override,
            "images/generations",
        )
        payload = {
            "model": provider.model,
            "prompt": prompt,
            "size": size,
            "quality": quality,
            "background": background,
            "n": int(n),
        }
        if provider.auth_mode != "codex_oauth":
            payload["output_format"] = output_format
        return (
            url,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            _headers(
                auth_headers,
                content_type="application/json",
                accept="application/json",
            ),
            None,
        )

    assert base_image is not None
    if provider.auth_mode == "codex_oauth":
        payload = _oauth_edit_json_payload(
            provider=provider,
            base_image=base_image,
            references=references,
            mask_png=mask_png,
            prompt=prompt,
            size=size,
            quality=quality,
            n=int(n),
        )
        return (
            configured_endpoint_url(
                base_url,
                endpoint_override,
                "images/edits",
            ),
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            _headers(
                auth_headers,
                content_type="application/json",
                accept="application/json",
            ),
            "images",
        )
    body, content_type, image_field = _build_edit_multipart(
        provider=provider,
        base_image=base_image,
        references=references,
        mask_png=mask_png,
        prompt=prompt,
        size=size,
        quality=quality,
        background=background,
        output_format=output_format,
        n=int(n),
    )
    return (
        configured_endpoint_url(
            base_url,
            endpoint_override,
            "images/edits",
        ),
        body,
        _headers(
            auth_headers,
            content_type=content_type,
            accept="application/json",
        ),
        image_field,
    )


def _async_route_urls(
    sync_url: str,
    *,
    operation: str,
) -> tuple[str, str, str] | None:
    parsed = urlsplit(sync_url)
    path = parsed.path.rstrip("/")
    expected = (
        "/images/generations" if operation == "generate" else "/images/edits"
    )
    if path.lower().endswith(expected + "/async"):
        raise BridgeError(
            "Custom endpoint fields must contain the synchronous Images endpoint. "
            "Enable 'use_async' to derive its /async route.",
            error_code="async_endpoint_must_be_sync_route",
            operation=operation,
            protocol="images",
            endpoint=sync_url,
            retryable=False,
            safe_to_resubmit=True,
        )
    if not path.lower().endswith(expected):
        return None

    prefix = path[: -len(expected)]
    submit_path = path + "/async"
    task_root_path = prefix + "/images/tasks"
    submit_url = urlunsplit(
        (parsed.scheme, parsed.netloc, submit_path, parsed.query, "")
    )
    task_root_url = urlunsplit(
        (parsed.scheme, parsed.netloc, task_root_path, parsed.query, "")
    )
    task_template_url = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            task_root_path.rstrip("/") + "/{task_id}",
            parsed.query,
            "",
        )
    )
    return submit_url, task_root_url, task_template_url


def _resolve_async_routes(
    provider: ProviderConfig,
    *,
    sync_url: str,
    operation: str,
) -> tuple[str, str] | None:
    standard = _async_route_urls(sync_url, operation=operation)
    submit_override = (
        provider.async_generate_endpoint
        if operation == "generate"
        else provider.async_edit_endpoint
    )
    poll_override = provider.async_poll_endpoint_template
    if submit_override:
        submit_url = configured_endpoint_url(
            provider.base_url,
            submit_override,
            "unused",
        )
    elif standard is not None:
        submit_url = standard[0]
    else:
        submit_url = ""

    if poll_override:
        task_template_url = configured_endpoint_url(
            provider.base_url,
            poll_override,
            "unused",
        )
    elif standard is not None:
        task_template_url = standard[2]
    else:
        task_template_url = ""

    if not submit_url and not task_template_url:
        return None
    if not submit_url:
        raise BridgeError(
            "Custom async polling was configured, but no async submit endpoint "
            "could be derived. Fill the operation-specific async submit endpoint.",
            error_code="custom_async_submit_endpoint_missing",
            operation=operation,
            protocol="images",
            safe_to_resubmit=True,
        )
    if not task_template_url:
        raise BridgeError(
            "A custom async submit endpoint was configured, but no task polling "
            "endpoint could be derived. Fill the async poll endpoint template.",
            error_code="custom_async_poll_endpoint_missing",
            operation=operation,
            protocol="images",
            safe_to_resubmit=True,
        )
    return submit_url, task_template_url


_JSON_POINTER_MISSING = object()


def _json_pointer_value(root: Any, pointer: str) -> Any:
    if pointer == "":
        return root
    current = root
    for encoded_segment in pointer.split("/")[1:]:
        segment = encoded_segment.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if segment not in current:
                return _JSON_POINTER_MISSING
            current = current[segment]
            continue
        if isinstance(current, list):
            if not segment.isdigit():
                return _JSON_POINTER_MISSING
            index = int(segment)
            if index >= len(current):
                return _JSON_POINTER_MISSING
            current = current[index]
            continue
        return _JSON_POINTER_MISSING
    return current


def _first_mapped_value(root: Any, pointers: tuple[str, ...]) -> Any:
    for pointer in pointers:
        value = _json_pointer_value(root, pointer)
        if value is not _JSON_POINTER_MISSING and value is not None:
            return value
    return _JSON_POINTER_MISSING


def _mapping_scalar(
    root: Any,
    pointers: tuple[str, ...],
    *,
    field_name: str,
) -> Any:
    value = _first_mapped_value(root, pointers)
    if value is _JSON_POINTER_MISSING:
        return None
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        raise BridgeError(
            f"Custom async mapping for {field_name} resolved to a non-scalar value.",
            error_code="invalid_async_mapping_value",
            protocol="images",
            retryable=False,
        )
    return value


def _task_id_from_payload(
    payload: dict[str, Any],
    mapping: AsyncResponseMapping | None = None,
) -> str | None:
    if mapping is not None and mapping.task_id_paths is not None:
        candidates: list[Any] = [
            _mapping_scalar(
                payload,
                mapping.task_id_paths,
                field_name="task_id_paths",
            )
        ]
    else:
        candidates = [payload.get("task_id")]
        for name in ("data", "result"):
            nested = payload.get(name)
            if isinstance(nested, dict):
                candidates.append(nested.get("task_id"))
    for candidate in candidates:
        if candidate is None:
            continue
        task_id = str(candidate).strip()
        if not task_id:
            continue
        if (
            len(task_id) > ASYNC_TASK_ID_MAX_LENGTH
            or task_id in {".", ".."}
            or any(ord(character) < 32 or ord(character) == 127 for character in task_id)
        ):
            raise BridgeError(
                "Async Images API returned an invalid task identifier.",
                error_code="invalid_async_task_id",
                protocol="images",
                phase="async_submit",
                retryable=False,
                safe_to_resubmit=False,
                remote_task_may_continue=True,
            )
        return task_id
    return None


def _async_status(
    payload: dict[str, Any],
    mapping: AsyncResponseMapping | None = None,
) -> str:
    if mapping is not None and mapping.status_paths is not None:
        candidates: list[Any] = [
            _mapping_scalar(
                payload,
                mapping.status_paths,
                field_name="status_paths",
            )
        ]
    else:
        candidates = [payload.get("status")]
        for name in ("data", "result"):
            nested = payload.get(name)
            if isinstance(nested, dict):
                candidates.append(nested.get("status"))
    for candidate in candidates:
        if candidate is not None:
            return str(candidate).strip().lower().replace("-", "_").replace(" ", "_")
    return ""


def _default_async_result_value(payload: dict[str, Any]) -> Any:
    result = payload.get("result")
    if isinstance(result, (dict, list)):
        return result
    data = payload.get("data")
    if isinstance(data, list) and data:
        return payload
    return _JSON_POINTER_MISSING


def _default_image_items(result: Any) -> Any:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            return data
        return [result]
    return _JSON_POINTER_MISSING


def _mapped_image_scalar(
    item: Any,
    pointers: tuple[str, ...] | None,
    *,
    default_keys: tuple[str, ...],
) -> str | None:
    if pointers is not None:
        value = _mapping_scalar(item, pointers, field_name="image output")
        return str(value) if value is not None else None
    if isinstance(item, str):
        is_url = item.lower().startswith(("http://", "https://"))
        if "url" in default_keys or "image_url" in default_keys:
            return item if is_url else None
        return item if not is_url else None
    if not isinstance(item, dict):
        return None
    for key in default_keys:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _project_async_result(
    result: Any,
    mapping: AsyncResponseMapping,
) -> dict[str, Any] | None:
    if mapping.image_items_paths is not None:
        items = _first_mapped_value(result, mapping.image_items_paths)
    else:
        items = _default_image_items(result)
    if items is _JSON_POINTER_MISSING or items is None:
        return None
    if not isinstance(items, list):
        items = [items]

    entries: list[dict[str, Any]] = []
    for item in items:
        if (
            isinstance(item, str)
            and mapping.image_b64_paths is None
            and mapping.image_url_paths is None
        ):
            entry = (
                {"url": item}
                if item.lower().startswith(("http://", "https://"))
                else {"b64_json": item}
            )
            entries.append(entry)
            continue
        b64_value = _mapped_image_scalar(
            item,
            mapping.image_b64_paths,
            default_keys=("b64_json", "base64", "b64"),
        )
        url_value = _mapped_image_scalar(
            item,
            mapping.image_url_paths,
            default_keys=("url", "image_url"),
        )
        revised = _mapped_image_scalar(
            item,
            mapping.revised_prompt_paths,
            default_keys=("revised_prompt",),
        )
        entry: dict[str, Any] = {}
        if b64_value:
            if b64_value.lower().startswith(("http://", "https://")) and not url_value:
                entry["url"] = b64_value
            else:
                entry["b64_json"] = b64_value
        elif url_value:
            entry["url"] = url_value
        if revised:
            entry["revised_prompt"] = revised
        if entry.get("b64_json") or entry.get("url"):
            entries.append(entry)
    return {"data": entries} if entries else None


def _async_result_payload(
    payload: dict[str, Any],
    mapping: AsyncResponseMapping | None = None,
) -> dict[str, Any] | None:
    if mapping is None:
        result = payload.get("result")
        if isinstance(result, dict):
            return result
        data = payload.get("data")
        if isinstance(data, list) and data:
            return payload
        return None

    if mapping.result_paths is not None:
        result = _first_mapped_value(payload, mapping.result_paths)
    else:
        result = _default_async_result_value(payload)
    if result is _JSON_POINTER_MISSING:
        return None
    return _project_async_result(result, mapping)


def _bounded_async_error_field(
    value: Any,
    *,
    known_secrets: tuple[str, ...],
    max_chars: int,
    fallback: str | None = None,
) -> str | None:
    if value is None or value == "":
        if fallback is None:
            return None
        value = fallback
    text = str(value)
    # Replace exact opaque credentials before truncating so a boundary cannot
    # leave a partial credential in the diagnostic string.
    for secret in known_secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    if len(text) > max_chars:
        text = text[:max_chars] + "…[truncated]"
    return redact_text(text, known_secrets)


def _async_failure_fields(
    payload: dict[str, Any],
    mapping: AsyncResponseMapping | None = None,
    *,
    known_secrets: tuple[str, ...] = (),
) -> tuple[str, str | None, str | None]:
    if mapping is not None and mapping.error_message_paths is not None:
        message = _mapping_scalar(
            payload,
            mapping.error_message_paths,
            field_name="error_message_paths",
        )
    else:
        error = payload.get("error")
        if not isinstance(error, dict):
            error = {}
        message = (
            error.get("message")
            or error.get("detail")
            or payload.get("message")
        )
    if mapping is not None and mapping.error_type_paths is not None:
        error_type = _mapping_scalar(
            payload,
            mapping.error_type_paths,
            field_name="error_type_paths",
        )
    else:
        error = payload.get("error")
        error_type = error.get("type") if isinstance(error, dict) else None
    if mapping is not None and mapping.error_code_paths is not None:
        error_code = _mapping_scalar(
            payload,
            mapping.error_code_paths,
            field_name="error_code_paths",
        )
    else:
        error = payload.get("error")
        error_code = error.get("code") if isinstance(error, dict) else None
    return (
        _bounded_async_error_field(
            message,
            known_secrets=known_secrets,
            max_chars=_ASYNC_ERROR_MESSAGE_MAX_CHARS,
            fallback="Async image task failed.",
        )
        or "Async image task failed.",
        _bounded_async_error_field(
            error_type,
            known_secrets=known_secrets,
            max_chars=_ASYNC_ERROR_FIELD_MAX_CHARS,
        ),
        _bounded_async_error_field(
            error_code,
            known_secrets=known_secrets,
            max_chars=_ASYNC_ERROR_FIELD_MAX_CHARS,
        ),
    )


def _async_status_sets(
    mapping: AsyncResponseMapping | None,
) -> tuple[set[str], set[str]]:
    success = set(
        mapping.success_statuses
        if mapping is not None and mapping.success_statuses is not None
        else _ASYNC_SUCCESS_STATUSES
    )
    failure = set(
        mapping.failure_statuses
        if mapping is not None and mapping.failure_statuses is not None
        else _ASYNC_FAILURE_STATUSES
    )
    overlap = success.intersection(failure)
    if overlap:
        raise BridgeError(
            "Custom async success and failure status sets overlap.",
            error_code="invalid_async_status_mapping",
            protocol="images",
            retryable=False,
            safe_to_resubmit=True,
        )
    return success, failure


def _task_url_from_template(task_template_url: str, task_id: str) -> str:
    return task_template_url.replace("{task_id}", quote(task_id, safe=""), 1)


def _json_payload_response(
    response: HttpResponse,
    payload: dict[str, Any],
    *,
    url: str | None = None,
) -> HttpResponse:
    return HttpResponse(
        status=response.status,
        headers=response.headers,
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        elapsed_ms=response.elapsed_ms,
        attempts=response.attempts,
        url=url or response.url,
    )


def _request_evidence(
    response: HttpResponse,
    *,
    phase: str,
    image_field: str | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "phase": phase,
        "endpoint": response.url,
        "http_status": response.status,
        "request_id": response.request_id,
        "attempts": response.attempts,
        "elapsed_ms": response.elapsed_ms,
    }
    if image_field is not None:
        evidence["multipart_image_field"] = image_field
    return evidence


def _async_fallback_allowed(error: BridgeError) -> bool:
    return error.status in _ASYNC_UNSUPPORTED_STATUSES


def _hide_task_id_in_error(
    error: BridgeError,
    *,
    task_id: str,
    task_template_url: str,
    phase: str,
    remote_task_may_continue: bool,
) -> None:
    for name in (
        "message",
        "request_id",
        "error_type",
        "error_code",
        "moderation_category",
        "retry_after",
    ):
        value = getattr(error, name)
        if isinstance(value, str):
            setattr(error, name, _hide_task_id_text(value, task_id=task_id))
    error.endpoint = task_template_url
    error.phase = phase
    error.safe_to_resubmit = False
    error.remote_task_may_continue = remote_task_may_continue


def _hide_task_id_in_evidence(
    evidence: list[dict[str, Any]],
    *,
    task_id: str,
) -> None:
    for item in evidence:
        for name, value in tuple(item.items()):
            if isinstance(value, str):
                item[name] = _hide_task_id_text(value, task_id=task_id)


def _hide_task_id_text(value: str, *, task_id: str) -> str:
    result = value.replace(task_id, "{task_id}")
    encoded_task_id = quote(task_id, safe="")
    if encoded_task_id:
        result = re.sub(
            re.escape(encoded_task_id),
            "{task_id}",
            result,
            flags=re.IGNORECASE,
        )
    return result


def _sync_images_request(
    *,
    client: HttpClient,
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: int,
    secrets: tuple[str, ...],
    operation: str,
    image_field: str | None,
    max_attempts: int = 1,
    phase: str = "sync",
    deadline: float | None = None,
) -> tuple[
    list[bytes],
    str,
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    request_completed = False
    try:
        response = client.request(
            url=url,
            body=body,
            headers=headers,
            timeout=timeout,
            max_attempts=max_attempts,
            known_secrets=secrets,
            operation=operation,
            protocol="images",
        )
        request_completed = True
        images, revised_prompt, metadata = _parse_images(
            response,
            client=client,
            timeout=timeout,
            known_secrets=secrets,
            deadline=deadline,
        )
    except BridgeError as error:
        if phase == "sync_fallback":
            error.phase = phase
            error.safe_to_resubmit = False
            error.remote_task_may_continue = (
                not request_completed
                and (
                    error.status is None
                    or error.status in {408, 409, 429, 500, 502, 503, 504}
                )
            )
            _redact_bridge_error(error, secrets)
        raise
    return (
        images,
        revised_prompt,
        metadata,
        [_request_evidence(response, phase=phase, image_field=image_field)],
    )


def _remaining_async_timeout(
    client: HttpClient,
    *,
    deadline: float,
    operation: str,
    endpoint: str,
    phase: str,
    remote_task_may_continue: bool,
) -> int:
    remaining = deadline - client.monotonic()
    if remaining <= 0:
        raise BridgeError(
            "Async image operation reached timeout_sec before the next step.",
            error_code="async_operation_timeout",
            operation=operation,
            protocol="images",
            endpoint=endpoint,
            retryable=False,
            phase=phase,
            safe_to_resubmit=False,
            remote_task_may_continue=remote_task_may_continue,
        )
    return max(1, math.ceil(remaining))


def _execute_images_request(
    *,
    provider: ProviderConfig,
    client: HttpClient,
    operation: str,
    sync_url: str,
    body: bytes,
    headers: dict[str, str],
    image_field: str | None,
    timeout: int,
    secrets: tuple[str, ...],
) -> tuple[
    list[bytes],
    str,
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    async_report: dict[str, Any] = {
        "requested": bool(provider.use_async),
        "attempted": False,
        "effective_mode": "sync",
    }
    if provider.auth_mode != "api_key":
        async_report["skipped_reason"] = "Codex OAuth uses its verified synchronous routes."
        result = _sync_images_request(
            client=client,
            url=sync_url,
            body=body,
            headers=headers,
            timeout=timeout,
            secrets=secrets,
            operation=operation,
            image_field=image_field,
        )
        return (*result, async_report)
    routes = _resolve_async_routes(
        provider,
        sync_url=sync_url,
        operation=operation,
    )
    if not provider.use_async:
        async_report["skipped_reason"] = "Disabled by the API Provider switch."
        result = _sync_images_request(
            client=client,
            url=sync_url,
            body=body,
            headers=headers,
            timeout=timeout,
            secrets=secrets,
            operation=operation,
            image_field=image_field,
        )
        return (*result, async_report)

    if routes is None:
        async_report["skipped_reason"] = (
            "The effective custom endpoint is not a standard Images generations/edits "
            "route, so no async path was guessed."
        )
        result = _sync_images_request(
            client=client,
            url=sync_url,
            body=body,
            headers=headers,
            timeout=timeout,
            secrets=secrets,
            operation=operation,
            image_field=image_field,
        )
        return (*result, async_report)

    submit_url, task_template_url = routes
    success_statuses, failure_statuses = _async_status_sets(provider.async_mapping)
    async_report.update(
        {
            "attempted": True,
            "effective_mode": "async",
            "poll_interval_sec": ASYNC_POLL_INTERVAL_SECONDS,
            "contract": (
                "custom"
                if _custom_async_requested(provider, operation=operation)
                else "standard"
            ),
            "mapping_mode": (
                "custom" if provider.async_mapping is not None else "standard"
            ),
        }
    )
    request_evidence: list[dict[str, Any]] = []
    deadline = client.monotonic() + max(1, int(timeout))
    _check_comfy_interrupted()
    try:
        submit_response = client.request(
            url=submit_url,
            body=body,
            headers=headers,
            timeout=max(1, int(timeout)),
            max_attempts=1,
            known_secrets=secrets,
            operation=operation,
            protocol="images",
        )
    except BridgeError as error:
        if _async_fallback_allowed(error):
            _check_comfy_interrupted()
            fallback_timeout = _remaining_async_timeout(
                client,
                deadline=deadline,
                operation=operation,
                endpoint=sync_url,
                phase="sync_fallback",
                remote_task_may_continue=False,
            )
            async_report.update(
                {
                    "effective_mode": "sync_fallback",
                    "fallback_reason": "Async endpoint explicitly unsupported.",
                    "fallback_http_status": error.status,
                    "fallback_error_code": error.error_code,
                }
            )
            request_evidence.append(
                {
                    "phase": "async_submit",
                    "endpoint": error.endpoint,
                    "http_status": error.status,
                    "request_id": error.request_id,
                    "attempts": error.attempts,
                    "outcome": "unsupported",
                }
            )
            sync_result = _sync_images_request(
                client=client,
                url=sync_url,
                body=body,
                headers=headers,
                timeout=fallback_timeout,
                secrets=secrets,
                operation=operation,
                image_field=image_field,
                max_attempts=1,
                phase="sync_fallback",
                deadline=deadline,
            )
            images, revised_prompt, metadata, sync_evidence = sync_result
            return (
                images,
                revised_prompt,
                metadata,
                request_evidence + sync_evidence,
                async_report,
            )
        error.phase = "async_submit"
        error.safe_to_resubmit = False
        error.remote_task_may_continue = (
            error.status is None
            or error.status in {408, 409, 429, 500, 502, 503, 504}
        )
        _redact_bridge_error(error, secrets)
        raise

    request_evidence.append(
        _request_evidence(
            submit_response,
            phase="async_submit",
            image_field=image_field,
        )
    )
    async_report["submit_elapsed_ms"] = submit_response.elapsed_ms
    _check_comfy_interrupted()
    try:
        submit_payload = _json_response(
            submit_response,
            "Async Images submit endpoint",
        )
    except BridgeError as error:
        error.phase = "async_submit"
        error.safe_to_resubmit = False
        error.remote_task_may_continue = True
        _redact_bridge_error(error, secrets)
        raise

    task_id: str | None = None
    try:
        task_id = _task_id_from_payload(
            submit_payload,
            provider.async_mapping,
        )
        submit_status = _async_status(
            submit_payload,
            provider.async_mapping,
        )
        immediate_payload = _async_result_payload(
            submit_payload,
            provider.async_mapping,
        )
    except BridgeError as error:
        if error.status is None:
            error.status = submit_response.status
        if error.error_code == "invalid_async_task_id":
            # The response request ID may echo the invalid task candidate.
            error.request_id = (
                "[REDACTED]" if submit_response.request_id is not None else None
            )
        elif error.request_id is None:
            error.request_id = submit_response.request_id
        if error.operation is None:
            error.operation = operation
        if error.endpoint is None:
            error.endpoint = submit_response.url
        error.attempts = max(error.attempts, submit_response.attempts)
        if task_id is not None:
            _hide_task_id_in_error(
                error,
                task_id=task_id,
                task_template_url=task_template_url,
                phase="async_submit",
                remote_task_may_continue=True,
            )
        else:
            error.phase = "async_submit"
            error.safe_to_resubmit = False
            error.remote_task_may_continue = True
        _redact_bridge_error(error, secrets)
        raise
    if immediate_payload is not None and (
        submit_status not in failure_statuses
        and (task_id is None or submit_status in success_statuses)
    ):
        immediate_response = _json_payload_response(
            submit_response,
            immediate_payload,
        )
        try:
            result_timeout = _remaining_async_timeout(
                client,
                deadline=deadline,
                operation=operation,
                endpoint=submit_response.url,
                phase="async_result",
                remote_task_may_continue=task_id is not None,
            )
            images, revised_prompt, metadata = _parse_images(
                immediate_response,
                client=client,
                timeout=result_timeout,
                known_secrets=secrets,
                deadline=deadline,
            )
        except BridgeError as error:
            if task_id is not None:
                _hide_task_id_in_error(
                    error,
                    task_id=task_id,
                    task_template_url=task_template_url,
                    phase="async_result",
                    remote_task_may_continue=False,
                )
            else:
                error.phase = "async_result"
                error.safe_to_resubmit = False
                error.remote_task_may_continue = False
            raise
        async_report.update(
            {
                "final_task_status": submit_status or "immediate_result",
                "poll_count": 0,
                "transient_poll_errors": 0,
                "poll_elapsed_ms": 0,
            }
        )
        if task_id is not None:
            async_report["task_id"] = {
                "present": True,
                "sha256": hashlib.sha256(task_id.encode("utf-8")).hexdigest(),
            }
            _hide_task_id_in_evidence(request_evidence, task_id=task_id)
            _hide_task_id_in_evidence(metadata, task_id=task_id)
            revised_prompt = _hide_task_id_text(
                revised_prompt,
                task_id=task_id,
            )
        return images, revised_prompt, metadata, request_evidence, async_report

    if submit_status in failure_statuses:
        try:
            message, error_type, error_code = _async_failure_fields(
                submit_payload,
                provider.async_mapping,
                known_secrets=secrets,
            )
        except BridgeError as error:
            if error.status is None:
                error.status = submit_response.status
            if error.request_id is None:
                error.request_id = submit_response.request_id
            if error.operation is None:
                error.operation = operation
            if error.endpoint is None:
                error.endpoint = submit_response.url
            error.attempts = max(error.attempts, submit_response.attempts)
            error.phase = "async_submit"
            error.safe_to_resubmit = False
            error.remote_task_may_continue = False
            if task_id is not None:
                _hide_task_id_in_error(
                    error,
                    task_id=task_id,
                    task_template_url=task_template_url,
                    phase="async_submit",
                    remote_task_may_continue=False,
                )
            _redact_bridge_error(error, secrets)
            raise
        if task_id is not None:
            message = _hide_task_id_text(message, task_id=task_id)
            if isinstance(error_type, str):
                error_type = _hide_task_id_text(error_type, task_id=task_id)
            if isinstance(error_code, str):
                error_code = _hide_task_id_text(error_code, task_id=task_id)
        error = BridgeError(
            redact_text(message, secrets),
            status=submit_response.status,
            request_id=submit_response.request_id,
            error_type=redact_text(error_type, secrets) if error_type else None,
            error_code=redact_text(error_code, secrets) if error_code else None,
            operation=operation,
            protocol="images",
            endpoint=submit_response.url,
            retryable=False,
            attempts=submit_response.attempts,
            phase="async_submit",
            safe_to_resubmit=False,
            remote_task_may_continue=False,
        )
        if task_id is not None:
            _hide_task_id_in_error(
                error,
                task_id=task_id,
                task_template_url=task_template_url,
                phase="async_submit",
                remote_task_may_continue=False,
            )
        raise error
    if task_id is None:
        raise BridgeError(
            "Async Images submit endpoint returned neither image data nor a task_id. "
            "No synchronous retry was attempted because remote acceptance is unknown.",
            status=submit_response.status,
            request_id=submit_response.request_id,
            error_code="invalid_async_submit_response",
            operation=operation,
            protocol="images",
            endpoint=submit_response.url,
            retryable=False,
            attempts=submit_response.attempts,
            phase="async_submit",
            safe_to_resubmit=False,
            remote_task_may_continue=True,
        )

    task_descriptor = {
        "present": True,
        "sha256": hashlib.sha256(task_id.encode("utf-8")).hexdigest(),
    }
    async_report["task_id"] = task_descriptor
    _hide_task_id_in_evidence(request_evidence, task_id=task_id)
    task_url = _task_url_from_template(task_template_url, task_id)
    poll_started = client.monotonic()
    poll_count = 0
    transient_poll_errors = 0
    last_status = submit_status

    while True:
        _check_comfy_interrupted()
        remaining = deadline - client.monotonic()
        if remaining <= 0:
            raise BridgeError(
                "Async image task did not finish before timeout_sec. The remote task "
                "may still be running; no synchronous retry was attempted.",
                error_code="async_poll_timeout",
                operation=operation,
                protocol="images",
                endpoint=task_template_url,
                retryable=False,
                phase="async_poll",
                safe_to_resubmit=False,
                remote_task_may_continue=True,
            )
        client.sleep(min(ASYNC_POLL_INTERVAL_SECONDS, remaining))
        _check_comfy_interrupted()
        remaining = deadline - client.monotonic()
        if remaining <= 0:
            continue

        poll_count += 1
        try:
            poll_response = client.request(
                url=task_url,
                method="GET",
                headers=_headers(
                    {
                        name: value
                        for name, value in headers.items()
                        if name.lower() == "authorization"
                    },
                    content_type="application/json",
                    accept="application/json",
                ),
                timeout=max(1, min(30, math.ceil(remaining))),
                max_attempts=1,
                known_secrets=secrets,
                operation=operation,
                protocol="images",
            )
        except BridgeError as error:
            _hide_task_id_in_error(
                error,
                task_id=task_id,
                task_template_url=task_template_url,
                phase="async_poll",
                remote_task_may_continue=True,
            )
            _redact_bridge_error(error, secrets)
            if error.retryable and client.monotonic() < deadline:
                transient_poll_errors += 1
                continue
            raise

        try:
            poll_payload = _json_response(
                poll_response,
                "Async Images task endpoint",
            )
        except BridgeError as error:
            _hide_task_id_in_error(
                error,
                task_id=task_id,
                task_template_url=task_template_url,
                phase="async_poll",
                remote_task_may_continue=True,
            )
            _redact_bridge_error(error, secrets)
            raise

        try:
            last_status = _async_status(
                poll_payload,
                provider.async_mapping,
            )
            result_payload = _async_result_payload(
                poll_payload,
                provider.async_mapping,
            )
        except BridgeError as error:
            if error.status is None:
                error.status = poll_response.status
            if error.request_id is None:
                error.request_id = poll_response.request_id
            if error.operation is None:
                error.operation = operation
            error.attempts = max(error.attempts, poll_count)
            _hide_task_id_in_error(
                error,
                task_id=task_id,
                task_template_url=task_template_url,
                phase="async_poll",
                remote_task_may_continue=True,
            )
            _redact_bridge_error(error, secrets)
            raise
        if last_status in success_statuses and result_payload is not None:
            result_response = _json_payload_response(
                poll_response,
                result_payload,
                url=task_template_url,
            )
            try:
                result_timeout = _remaining_async_timeout(
                    client,
                    deadline=deadline,
                    operation=operation,
                    endpoint=task_template_url,
                    phase="async_result",
                    remote_task_may_continue=False,
                )
                images, revised_prompt, metadata = _parse_images(
                    result_response,
                    client=client,
                    timeout=result_timeout,
                    known_secrets=secrets,
                    deadline=deadline,
                )
            except BridgeError as error:
                _hide_task_id_in_error(
                    error,
                    task_id=task_id,
                    task_template_url=task_template_url,
                    phase="async_result",
                    remote_task_may_continue=False,
                )
                raise
            async_report.update(
                {
                    "final_task_status": last_status,
                    "poll_count": poll_count,
                    "transient_poll_errors": transient_poll_errors,
                    "poll_elapsed_ms": round(
                        (client.monotonic() - poll_started) * 1000
                    ),
                }
            )
            poll_evidence = {
                "phase": "async_poll",
                "endpoint": task_template_url,
                "http_status": poll_response.status,
                "request_id": poll_response.request_id,
                "attempts": poll_count,
                "poll_count": poll_count,
                "transient_errors": transient_poll_errors,
                "elapsed_ms": async_report["poll_elapsed_ms"],
            }
            request_evidence.append(poll_evidence)
            _hide_task_id_in_evidence(request_evidence, task_id=task_id)
            _hide_task_id_in_evidence(metadata, task_id=task_id)
            revised_prompt = _hide_task_id_text(
                revised_prompt,
                task_id=task_id,
            )
            return images, revised_prompt, metadata, request_evidence, async_report

        if last_status in failure_statuses:
            try:
                message, error_type, error_code = _async_failure_fields(
                    poll_payload,
                    provider.async_mapping,
                    known_secrets=secrets,
                )
            except BridgeError as error:
                if error.status is None:
                    error.status = poll_response.status
                if error.request_id is None:
                    error.request_id = poll_response.request_id
                if error.operation is None:
                    error.operation = operation
                error.attempts = max(error.attempts, poll_count)
                _hide_task_id_in_error(
                    error,
                    task_id=task_id,
                    task_template_url=task_template_url,
                    phase="async_poll",
                    remote_task_may_continue=False,
                )
                _redact_bridge_error(error, secrets)
                raise
            message = _hide_task_id_text(message, task_id=task_id)
            if isinstance(error_type, str):
                error_type = _hide_task_id_text(error_type, task_id=task_id)
            if isinstance(error_code, str):
                error_code = _hide_task_id_text(error_code, task_id=task_id)
            error = BridgeError(
                redact_text(message, secrets),
                status=poll_response.status,
                request_id=poll_response.request_id,
                error_type=redact_text(error_type, secrets) if error_type else None,
                error_code=redact_text(error_code, secrets) if error_code else None,
                operation=operation,
                protocol="images",
                endpoint=task_template_url,
                retryable=False,
                attempts=poll_count,
                phase="async_poll",
                safe_to_resubmit=False,
                remote_task_may_continue=False,
            )
            _hide_task_id_in_error(
                error,
                task_id=task_id,
                task_template_url=task_template_url,
                phase="async_poll",
                remote_task_may_continue=False,
            )
            raise error
        # Pending, unknown, and success-without-result all keep polling the
        # already-created task. None of them can trigger another paid POST.


def _base_report(
    *,
    provider: ProviderConfig,
    operation: str,
    protocol: str,
    selection_basis: str,
    prompt: str,
    base_image: EncodedImage | None,
    references: tuple[EncodedImage, ...],
    mask_png: bytes | None,
) -> dict[str, Any]:
    ordered_images = []
    if base_image is not None:
        ordered_images.append(base_image.public_dict())
    ordered_images.extend(image.public_dict() for image in references)
    return {
        "ok": True,
        "provider": provider.public_dict(),
        "operation": operation,
        "requested_protocol": provider.api_protocol,
        "selected_protocol": protocol,
        "protocol_selection_basis": selection_basis,
        "input": {
            "roles": ordered_images,
            "prompt": _prompt_descriptor(prompt),
            "mask": {"present": mask_png is not None, "encoded_bytes": len(mask_png or b"")},
        },
    }


def _execute_operation(
    *,
    provider: ProviderConfig,
    operation: str,
    prompt: str,
    base_image: EncodedImage | None = None,
    references: tuple[EncodedImage, ...] = (),
    mask_png: bytes | None = None,
    size: str = "auto",
    quality: str = "auto",
    background: str = "auto",
    output_format: str = "png",
    moderation: str = "auto",
    n: int = 1,
    timeout: int = 300,
    client: HttpClient | None = None,
) -> OperationResult:
    if operation not in {"generate", "edit"}:
        raise ValueError("operation must be generate or edit.")
    if operation == "edit" and base_image is None:
        raise ValueError("Edit requires a base image.")
    if operation == "generate" and base_image is not None:
        raise ValueError("Generate does not accept a base image.")
    references = renumber_references(
        references,
        start_slot=2 if operation == "edit" else 1,
    )
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt cannot be empty.")
    if not 1 <= int(n) <= 8:
        raise ValueError("n must be between 1 and 8.")
    client = client or HttpClient()
    protocol, selection_basis = select_protocol(
        provider,
        operation=operation,
        has_references=bool(references),
        has_mask=mask_png is not None,
    )
    base_url, auth_headers, secrets = _auth_context(provider, timeout=timeout)
    report = _base_report(
        provider=provider,
        operation=operation,
        protocol=protocol,
        selection_basis=selection_basis,
        prompt=prompt,
        base_image=base_image,
        references=references,
        mask_png=mask_png,
    )
    started = time.monotonic()
    all_images: list[bytes] = []
    all_metadata: list[dict[str, Any]] = []
    revised_prompt = ""
    request_evidence: list[dict[str, Any]] = []
    endpoint_override = (
        provider.generate_endpoint if operation == "generate" else provider.edit_endpoint
    )

    if protocol == "responses":
        url = configured_endpoint_url(base_url, endpoint_override, "responses")
        payload = _responses_payload(
            operation=operation,
            provider=provider,
            prompt=prompt,
            base_image=base_image,
            references=references,
            size=size,
            quality=quality,
            background=background,
            output_format=output_format,
            moderation=moderation,
        )
        for request_index in range(int(n)):
            response = client.request(
                url=url,
                body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                headers=_headers(
                    auth_headers,
                    content_type="application/json",
                    accept="text/event-stream, application/json",
                ),
                timeout=timeout,
                known_secrets=secrets,
                operation=operation,
                protocol=protocol,
                max_attempts=1,
            )
            images, revised, metadata = _parse_responses(
                response,
                known_secrets=secrets,
            )
            all_images.extend(images)
            all_metadata.extend(metadata)
            revised_prompt = revised_prompt or revised
            request_evidence.append(
                {
                    "index": request_index + 1,
                    "endpoint": response.url,
                    "http_status": response.status,
                    "request_id": response.request_id,
                    "attempts": response.attempts,
                    "elapsed_ms": response.elapsed_ms,
                }
            )
    elif protocol == "chat_completions":
        url = configured_endpoint_url(
            base_url,
            endpoint_override,
            "chat/completions",
        )
        payload = _chat_completions_payload(
            operation=operation,
            provider=provider,
            prompt=prompt,
            base_image=base_image,
            references=references,
            size=size,
        )
        for request_index in range(int(n)):
            response = client.request(
                url=url,
                body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                headers=_headers(
                    auth_headers,
                    content_type="application/json",
                    accept="application/json",
                ),
                timeout=timeout,
                known_secrets=secrets,
                operation=operation,
                protocol=protocol,
                max_attempts=1,
            )
            images, revised, metadata = _parse_chat_completions(
                response,
                client=client,
                timeout=timeout,
                known_secrets=secrets,
            )
            all_images.extend(images)
            all_metadata.extend(metadata)
            revised_prompt = revised_prompt or revised
            request_evidence.append(
                {
                    "index": request_index + 1,
                    "endpoint": response.url,
                    "http_status": response.status,
                    "request_id": response.request_id,
                    "attempts": response.attempts,
                    "elapsed_ms": response.elapsed_ms,
                }
            )
        report["protocol_notes"] = {
            "request_format": (
                "messages[].content: text, then base Image 1 and ordered references"
                if operation == "edit"
                else "messages[].content: text, then Reference 1, Reference 2, ..."
            ),
            "size_transport": (
                "prompt_instruction" if size != "auto" else "provider_default"
            ),
            "ignored_options": [
                "quality",
                "background",
                "output_format",
                "moderation",
            ],
            "n_transport": "one request per requested image",
        }
    else:
        url, body, image_headers, image_field = _images_request(
            provider=provider,
            operation=operation,
            base_url=base_url,
            endpoint_override=endpoint_override,
            auth_headers=auth_headers,
            prompt=prompt,
            base_image=base_image,
            references=references,
            mask_png=mask_png,
            size=size,
            quality=quality,
            background=background,
            output_format=output_format,
            n=int(n),
        )
        (
            all_images,
            revised_prompt,
            all_metadata,
            request_evidence,
            async_report,
        ) = _execute_images_request(
            provider=provider,
            client=client,
            operation=operation,
            sync_url=url,
            body=body,
            headers=image_headers,
            image_field=image_field,
            timeout=timeout,
            secrets=secrets,
        )
        report["async"] = async_report
        if provider.auth_mode == "codex_oauth":
            report["protocol_notes"] = {
                "effective_response_format": "b64_json",
                "ignored_options": ["background", "output_format"],
                "reason": (
                    "Native Codex OAuth image editing sends the ordered images "
                    "directly to the ChatGPT Codex backend, which returns b64_json "
                    "and does not accept output_format."
                ),
            }

    if protocol != "images":
        report["async"] = {
            "requested": bool(provider.use_async),
            "attempted": False,
            "effective_mode": "sync",
            "skipped_reason": (
                f"The selected {protocol} protocol has no Images async task contract."
            ),
        }

    report["requests"] = request_evidence
    report["output"] = {
        "count": len(all_images),
        "images": all_metadata,
        "revised_prompt_present": bool(revised_prompt),
    }
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    safe_report = sanitize(report, secrets)
    assert isinstance(safe_report, dict)
    return OperationResult(all_images, revised_prompt, safe_report)


def execute_operation(
    *,
    provider: ProviderConfig,
    operation: str,
    prompt: str,
    base_image: EncodedImage | None = None,
    references: tuple[EncodedImage, ...] = (),
    mask_png: bytes | None = None,
    size: str = "auto",
    quality: str = "auto",
    background: str = "auto",
    output_format: str = "png",
    moderation: str = "auto",
    n: int = 1,
    timeout: int = 300,
    client: HttpClient | None = None,
) -> OperationResult:
    """Execute one operation with a final secret-redaction boundary."""
    preflight_secrets = (
        (provider.api_key,)
        if provider.auth_mode == "api_key" and provider.api_key
        else ()
    )
    try:
        return _execute_operation(
            provider=provider,
            operation=operation,
            prompt=prompt,
            base_image=base_image,
            references=references,
            mask_png=mask_png,
            size=size,
            quality=quality,
            background=background,
            output_format=output_format,
            moderation=moderation,
            n=n,
            timeout=timeout,
            client=client,
        )
    except BridgeError as error:
        _redact_bridge_error(error, preflight_secrets)
        raise
