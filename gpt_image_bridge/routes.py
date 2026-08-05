from __future__ import annotations

from typing import Any

from .session_credentials import is_session_handle, issue_session_credential


ROUTE_PATH = "/gpt-image-bridge/session-credential"
MAX_REQUEST_BYTES = 20 * 1024
API_PROVIDER_CLASS = "GPTImageBridgeAPIProvider"


def credential_response(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object.")
    return {
        "credential_handle": issue_session_credential(payload.get("api_key", ""))
    }


def _redact_exact_string(value: Any, secret: str) -> None:
    """Remove a rejected credential from every string in the request payload."""
    if not secret:
        return
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if isinstance(item, str):
                value[key] = item.replace(secret, "[REDACTED]")
            else:
                _redact_exact_string(item, secret)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = item.replace(secret, "[REDACTED]")
            else:
                _redact_exact_string(item, secret)


def _scrub_serialized_workflow_keys(value: Any) -> None:
    """Clear API Key widget slots in any workflow metadata attached to /prompt."""
    if isinstance(value, dict):
        if value.get("type") == API_PROVIDER_CLASS:
            widgets = value.get("widgets_values")
            if isinstance(widgets, list) and widgets:
                widgets[0] = ""
        for item in value.values():
            _scrub_serialized_workflow_keys(item)
    elif isinstance(value, list):
        for item in value:
            _scrub_serialized_workflow_keys(item)


def guard_prompt_payload(json_data: Any) -> Any:
    """Fail closed before queue/history can persist a raw API credential."""
    if not isinstance(json_data, dict):
        return json_data
    prompt = json_data.get("prompt")
    reject_request = False
    if isinstance(prompt, dict):
        for node in prompt.values():
            if not isinstance(node, dict) or node.get("class_type") != API_PROVIDER_CLASS:
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            candidate = inputs.get("api_key")
            if candidate is None or (
                isinstance(candidate, str) and not candidate.strip()
            ):
                # An unconfigured API Provider carries no credential to leak.
                # It may be present on an unselected lazy branch (for example
                # while OAuth or a local backend is active), so it must not
                # invalidate the entire prompt. If that API branch is selected,
                # the provider itself reports credential_missing clearly.
                continue
            if isinstance(candidate, str) and is_session_handle(candidate):
                continue
            if isinstance(candidate, str):
                _redact_exact_string(json_data, candidate)
            inputs.pop("api_key", None)
            reject_request = True
    _scrub_serialized_workflow_keys(json_data)
    if reject_request:
        # Reject the whole request, not just this provider branch. ComfyUI may
        # skip validation for disconnected or partial-execution branches, and a
        # linked STRING source could otherwise survive in queue/history. Do not
        # raise: ComfyUI catches prompt-handler exceptions and continues with the
        # original request. An empty top-level payload takes the safe no-prompt
        # 400 path before queue insertion.
        json_data.clear()
    return json_data


def register_routes() -> bool:
    """Register the in-memory credential route when imported by ComfyUI."""
    try:
        from aiohttp import web
        from server import PromptServer
    except (ImportError, AttributeError):
        return False

    prompt_server = getattr(PromptServer, "instance", None)
    if prompt_server is None:
        return False

    route_marker = "_gpt_image_bridge_session_credential_route"
    if not getattr(prompt_server, route_marker, False):

        @prompt_server.routes.post(ROUTE_PATH)
        async def store_session_credential(request):
            if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
                return web.json_response(
                    {"error": "request_too_large"},
                    status=413,
                    headers={"Cache-Control": "no-store"},
                )
            try:
                result = credential_response(await request.json())
            except (ValueError, TypeError):
                return web.json_response(
                    {"error": "api_key_required"},
                    status=400,
                    headers={"Cache-Control": "no-store"},
                )
            return web.json_response(
                result,
                headers={"Cache-Control": "no-store"},
            )

        setattr(prompt_server, route_marker, True)

    guard_marker = "_gpt_image_bridge_prompt_guard"
    if not getattr(prompt_server, guard_marker, False):
        prompt_server.add_on_prompt_handler(guard_prompt_payload)
        setattr(prompt_server, guard_marker, True)
    return True
