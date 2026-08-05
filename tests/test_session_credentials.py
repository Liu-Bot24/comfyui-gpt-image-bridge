from __future__ import annotations

from pathlib import Path

import pytest

from gpt_image_bridge.errors import BridgeError
from gpt_image_bridge.routes import credential_response, guard_prompt_payload
from gpt_image_bridge.session_credentials import (
    HANDLE_PREFIX,
    SESSION_TTL_SECONDS,
    clear_session_credentials,
    consume_session_credential,
    issue_session_credential,
)


PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def empty_session_store():
    clear_session_credentials()
    yield
    clear_session_credentials()


def test_session_handle_resolves_without_containing_secret():
    secret = "example-provider-secret-value"
    handle = issue_session_credential(secret, now=10.0)
    assert handle.startswith(HANDLE_PREFIX)
    assert secret not in handle
    assert consume_session_credential(handle, now=11.0) == secret
    with pytest.raises(BridgeError, match="session_credential_missing"):
        consume_session_credential(handle, now=11.0)


def test_expired_or_unknown_session_handle_fails_clearly():
    handle = issue_session_credential("temporary-secret", now=10.0)
    with pytest.raises(BridgeError) as captured:
        consume_session_credential(handle, now=10.0 + SESSION_TTL_SECONDS + 1)
    assert captured.value.error_code == "session_credential_missing"
    assert "temporary-secret" not in str(captured.value)


def test_raw_api_clients_remain_supported_and_empty_route_is_rejected():
    assert consume_session_credential("direct-api-key") == "direct-api-key"
    with pytest.raises(BridgeError) as captured:
        consume_session_credential("direct-api-key", require_handle=True)
    assert captured.value.error_code == "session_credential_required"
    with pytest.raises(ValueError, match="cannot be empty"):
        credential_response({"api_key": ""})
    with pytest.raises(ValueError, match="JSON body"):
        credential_response([])


def test_frontend_contract_persists_local_workflow_key_but_queues_a_handle():
    source = (
        PROJECT / "gpt_image_bridge" / "web" / "api_key_widget.js"
    ).read_text(encoding="utf-8")
    assert "widget.serializeValue = async" in source
    assert 'input.type = "password"' in source
    assert "widget.type = PASSWORD_WIDGET_TYPE" in source
    assert "/gpt-image-bridge/session-credential" in source
    assert "credential_handle" in source
    assert "node.onSerialize" not in source
    assert 'widget.value = ""' not in source
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "widget.serialize = false" not in source


def test_concurrent_handles_do_not_cross_credentials():
    first = issue_session_credential("first-secret")
    second = issue_session_credential("second-secret")
    assert first != second
    assert consume_session_credential(second) == "second-secret"
    assert consume_session_credential(first) == "first-secret"


def test_prompt_guard_preserves_valid_session_handle_and_scrubs_workflow_slot():
    handle = issue_session_credential("temporary-secret")
    payload = {
        "prompt": {
            "1": {
                "class_type": "GPTImageBridgeAPIProvider",
                "inputs": {"api_key": handle},
            }
        },
        "extra_data": {
            "extra_pnginfo": {
                "workflow": {
                    "nodes": [
                        {
                            "type": "GPTImageBridgeAPIProvider",
                            "widgets_values": ["stale-workflow-key", "https://example.test/v1"],
                        }
                    ]
                }
            }
        },
    }
    result = guard_prompt_payload(payload)
    assert result["prompt"]["1"]["inputs"]["api_key"] == handle
    widgets = result["extra_data"]["extra_pnginfo"]["workflow"]["nodes"][0][
        "widgets_values"
    ]
    assert widgets == ["", "https://example.test/v1"]


@pytest.mark.parametrize(
    "candidate",
    [
        "raw-secret-value",
        "gpt-image-bridge-session:short",
        ["9", 0],
    ],
)
def test_prompt_guard_removes_non_handle_before_queue_validation(candidate):
    secret = candidate if isinstance(candidate, str) else "linked-secret-not-visible"
    payload = {
        "prompt": {
            "1": {
                "class_type": "GPTImageBridgeAPIProvider",
                "inputs": {"api_key": candidate, "model": f"model-{secret}"},
            },
            "9": {"class_type": "PrimitiveNode", "inputs": {"value": secret}},
        },
        "extra_data": {
            "extra_pnginfo": {
                "workflow": {
                    "nodes": [
                        {
                            "type": "GPTImageBridgeAPIProvider",
                            "widgets_values": [secret, "https://example.test/v1"],
                        }
                    ]
                }
            }
        },
    }
    result = guard_prompt_payload(payload)
    assert result == {}
    assert not secret or secret not in str(result)


@pytest.mark.parametrize("candidate", ["", "   ", None])
def test_prompt_guard_allows_unconfigured_api_provider_on_unselected_branch(
    candidate,
):
    payload = {
        "prompt": {
            "1": {
                "class_type": "GPTImageBridgeAPIProvider",
                "inputs": {"api_key": candidate},
            },
            "2": {
                "class_type": "GPTImageBridgeOAuthProvider",
                "inputs": {"model": "gpt-image-2"},
            },
        },
        "extra_data": {
            "extra_pnginfo": {
                "workflow": {
                    "nodes": [
                        {
                            "type": "GPTImageBridgeAPIProvider",
                            "widgets_values": [
                                "",
                                "https://example.test/v1",
                            ],
                        }
                    ]
                }
            }
        },
    }

    result = guard_prompt_payload(payload)

    assert "prompt" in result
    assert result["prompt"]["1"]["inputs"]["api_key"] == candidate
    widgets = result["extra_data"]["extra_pnginfo"]["workflow"]["nodes"][0][
        "widgets_values"
    ]
    assert widgets == ["", "https://example.test/v1"]
