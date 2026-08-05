from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

from gpt_image_bridge.config import with_protocol
from gpt_image_bridge.errors import BridgeError
from gpt_image_bridge.images import encode_reference_slots, tensor_to_png
from gpt_image_bridge.protocols import execute_operation, select_protocol
from gpt_image_bridge.transport import HttpClient

from conftest import FakeResponse, QueueOpener, image_tensor, png_b64, png_bytes


def test_auto_protocol_selection_is_deterministic(api_provider):
    assert select_protocol(
        api_provider, operation="generate", has_references=False
    )[0] == "images"
    assert select_protocol(
        api_provider, operation="generate", has_references=True
    )[0] == "responses"
    assert select_protocol(
        api_provider, operation="edit", has_references=False
    )[0] == "images"


def test_explicit_images_generation_with_refs_does_not_fallback(api_provider):
    with pytest.raises(BridgeError, match="no semantic fallback"):
        select_protocol(
            with_protocol(api_provider, "images"),
            operation="generate",
            has_references=True,
        )


def test_auto_protocol_uses_a_recognized_manual_chat_endpoint(api_provider):
    provider = replace(
        api_provider,
        api_protocol="auto",
        edit_endpoint="chat/completions",
    )
    protocol, basis = select_protocol(
        provider,
        operation="edit",
        has_references=True,
    )
    assert protocol == "chat_completions"
    assert "explicit edit endpoint" in basis


def test_explicit_protocol_conflicting_with_endpoint_fails(api_provider):
    provider = replace(
        api_provider,
        api_protocol="images",
        edit_endpoint="chat/completions",
    )
    with pytest.raises(BridgeError) as captured:
        select_protocol(
            provider,
            operation="edit",
            has_references=True,
        )
    assert captured.value.error_code == "endpoint_protocol_mismatch"


def test_images_endpoint_for_wrong_operation_fails(api_provider):
    provider = replace(
        api_provider,
        api_protocol="auto",
        edit_endpoint="images/generations",
    )
    with pytest.raises(BridgeError) as captured:
        select_protocol(
            provider,
            operation="edit",
            has_references=False,
        )
    assert captured.value.error_code == "endpoint_operation_mismatch"


def test_chat_completions_edit_preserves_base_then_reference_order(api_provider):
    provider = replace(
        api_provider,
        api_protocol="chat_completions",
        edit_endpoint="/v1/chat/completions",
    )
    base = tensor_to_png(
        image_tensor(9, 7, 0.1),
        source_slot=1,
        role="base",
    )
    references = encode_reference_slots(
        image_tensor(6, 8, 0.2),
        image_tensor(11, 5, 0.3),
    )
    response_payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + png_b64(12, 10)
                            },
                        }
                    ]
                }
            }
        ]
    }
    opener = QueueOpener(FakeResponse(json.dumps(response_payload).encode()))
    result = execute_operation(
        provider=provider,
        operation="edit",
        prompt="transfer identity only",
        base_image=base,
        references=references,
        size="1024x1024",
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )

    request = opener.requests[0][0]
    payload = json.loads(request.data)
    content = payload["messages"][0]["content"]
    assert request.full_url == "https://example.test/v1/chat/completions"
    assert payload["stream"] is False
    assert [part["type"] for part in content] == [
        "text",
        "image_url",
        "image_url",
        "image_url",
    ]
    assert [part["image_url"]["url"] for part in content[1:]] == [
        base.data_url,
        references[0].data_url,
        references[1].data_url,
    ]
    assert all(set(part["image_url"]) == {"url"} for part in content[1:])
    assert "Image 1 is the only base image" in content[0]["text"]
    assert "Requested output size: 1024x1024" in content[0]["text"]
    assert result.report["selected_protocol"] == "chat_completions"
    assert result.report["protocol_notes"]["size_transport"] == "prompt_instruction"
    assert [item["source_slot"] for item in result.report["input"]["roles"]] == [
        1,
        2,
        3,
    ]
    assert len(result.images) == 1


def test_chat_completions_generate_uses_custom_same_origin_endpoint(api_provider):
    provider = replace(
        api_provider,
        api_protocol="chat_completions",
        generate_endpoint="https://example.test/v1/vendor/generate-image",
    )
    response_payload = {
        "choices": [
            {
                "message": {
                    "images": [
                        {"b64_json": png_b64(7, 9)},
                    ]
                }
            }
        ]
    }
    opener = QueueOpener(FakeResponse(json.dumps(response_payload).encode()))
    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="generate",
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    assert opener.requests[0][0].full_url == (
        "https://example.test/v1/vendor/generate-image"
    )
    request_payload = json.loads(opener.requests[0][0].data)
    assert isinstance(request_payload["messages"][0]["content"], str)
    assert result.report["output"]["images"][0]["width"] == 7


def test_chat_completions_generate_uses_reference_local_numbers(api_provider):
    provider = replace(
        api_provider,
        api_protocol="chat_completions",
        generate_endpoint="chat/completions",
    )
    references = encode_reference_slots(
        image_tensor(6, 8, 0.2),
        None,
        image_tensor(11, 5, 0.3),
    )
    response_payload = {
        "choices": [
            {
                "message": {
                    "images": [{"b64_json": png_b64(7, 9)}],
                }
            }
        ]
    }
    opener = QueueOpener(FakeResponse(json.dumps(response_payload).encode()))
    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="combine the references into a new composition",
        references=references,
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )

    payload = json.loads(opener.requests[0][0].data)
    content = payload["messages"][0]["content"]
    assert "References 1 through 2" in content[0]["text"]
    assert "Images 1 through 2" not in content[0]["text"]
    assert [part["image_url"]["url"] for part in content[1:]] == [
        references[0].data_url,
        references[1].data_url,
    ]
    assert [
        item["source_slot"] for item in result.report["input"]["roles"]
    ] == [1, 2]
    assert result.report["protocol_notes"]["request_format"].endswith(
        "Reference 1, Reference 2, ..."
    )


def test_images_edit_compacts_legacy_reference_slots_after_base_image(api_provider):
    provider = with_protocol(api_provider, "images")
    base = tensor_to_png(image_tensor(9, 7, 0.1), source_slot=1, role="base")
    references = encode_reference_slots(
        image_tensor(6, 8, 0.2),
        None,
        image_tensor(11, 5, 0.3),
    )
    opener = QueueOpener(
        FakeResponse(
            json.dumps({"data": [{"b64_json": png_b64(7, 9)}]}).encode()
        )
    )
    result = execute_operation(
        provider=provider,
        operation="edit",
        prompt="edit the base using both references",
        base_image=base,
        references=references,
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )

    request_body = opener.requests[0][0].data
    assert b'name="image[]"; filename="image-1-base.png"' in request_body
    assert b'name="image[]"; filename="image-2-reference.png"' in request_body
    assert b'name="image[]"; filename="image-3-reference.png"' in request_body
    assert b'filename="image-4-reference.png"' not in request_body
    assert [item["source_slot"] for item in result.report["input"]["roles"]] == [
        1,
        2,
        3,
    ]


def test_chat_completions_markdown_url_download_has_no_authorization(api_provider):
    provider = with_protocol(api_provider, "chat_completions")
    payload = {
        "choices": [
            {
                "message": {
                    "content": "Result: ![generated](https://cdn.test/result.png)"
                }
            }
        ]
    }
    opener = QueueOpener(
        FakeResponse(json.dumps(payload).encode()),
        FakeResponse(png_bytes(13, 17), headers={"content-type": "image/png"}),
    )
    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="url",
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    assert result.report["output"]["images"][0]["height"] == 17
    assert "Authorization" not in opener.requests[1][0].headers


def test_chat_completions_n_uses_one_request_per_output(api_provider):
    provider = with_protocol(api_provider, "chat_completions")
    responses = [
        FakeResponse(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "image": {
                                    "url": "data:image/png;base64," + png_b64(8, 8)
                                }
                            }
                        }
                    ]
                }
            ).encode()
        )
        for _ in range(2)
    ]
    opener = QueueOpener(*responses)
    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="two",
        n=2,
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    assert len(opener.requests) == 2
    assert len(result.images) == 2
    for request, _timeout in opener.requests:
        assert "n" not in json.loads(request.data)


def test_chat_completions_mask_is_rejected_before_network(api_provider):
    provider = with_protocol(api_provider, "chat_completions")
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="edit",
            prompt="masked",
            base_image=tensor_to_png(image_tensor(8, 8), role="base"),
            mask_png=png_bytes(8, 8),
        )
    assert captured.value.error_code == "chat_completions_mask_unsupported"


def test_chat_completions_refusal_does_not_retry(api_provider):
    provider = with_protocol(api_provider, "chat_completions")
    payload = {
        "choices": [
            {
                "finish_reason": "content_filter",
                "message": {"content": "request refused"},
            }
        ]
    }
    opener = QueueOpener(FakeResponse(json.dumps(payload).encode()))
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="refused",
            client=HttpClient(opener=opener, sleeper=lambda _: None),
        )
    assert len(opener.requests) == 1
    assert captured.value.error_type == "safety_refusal"
    assert captured.value.retryable is False
    assert captured.value.protocol == "chat_completions"


def test_chat_completions_provider_text_cannot_leak_api_key(api_provider):
    provider = with_protocol(api_provider, "chat_completions")
    secret = api_provider.api_key
    payload = {
        "choices": [
            {
                "message": {
                    "content": f"No image was produced after receiving {secret}"
                }
            }
        ]
    }
    opener = QueueOpener(FakeResponse(json.dumps(payload).encode()))
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="redaction",
            client=HttpClient(opener=opener, sleeper=lambda _: None),
        )
    assert secret not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)


def test_plain_text_generation_images_json(api_provider, image_json):
    opener = QueueOpener(FakeResponse(image_json, headers={"content-type": "application/json"}))
    result = execute_operation(
        provider=api_provider,
        operation="generate",
        prompt="test prompt",
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    payload = json.loads(opener.requests[0][0].data)
    assert opener.requests[0][0].full_url.endswith("/v1/images/generations")
    assert payload["prompt"] == "test prompt"
    assert result.revised_prompt == "revised"
    assert result.report["selected_protocol"] == "images"
    assert len(result.images) == 1


def test_single_image_edit_without_references(api_provider, image_json):
    base = tensor_to_png(image_tensor(9, 7), role="base")
    opener = QueueOpener(FakeResponse(image_json))
    result = execute_operation(
        provider=api_provider,
        operation="edit",
        prompt="subtle edit",
        base_image=base,
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    request_body = opener.requests[0][0].data
    assert b'image-1-base.png' in request_body
    assert b"-reference.png" not in request_body
    assert result.report["input"]["roles"][0]["role"] == "base"


def test_oauth_edit_uses_native_json_transport_and_ordered_images(monkeypatch):
    from gpt_image_bridge.config import ProviderConfig

    access_token = "oauth-access-token-for-test"
    account_id = "oauth-account-id-for-test"
    monkeypatch.setattr(
        "gpt_image_bridge.protocols.oauth_request_context",
        lambda **_kwargs: (
            "https://chatgpt.com/backend-api/codex",
            {
                "Authorization": f"Bearer {access_token}",
                "chatgpt-account-id": account_id,
            },
            (access_token, account_id),
        ),
    )
    provider = ProviderConfig(
        auth_mode="codex_oauth",
        base_url="https://api.openai.com/v1",
        model="gpt-image-2",
        api_protocol="auto",
    )
    image_json = json.dumps({"data": [{"b64_json": png_b64()}]}).encode()
    opener = QueueOpener(FakeResponse(image_json))
    base = tensor_to_png(image_tensor(9, 7, 0.25), role="base")
    reference = tensor_to_png(
        image_tensor(5, 11, 0.75),
        role="reference",
        source_slot=2,
    )
    result = execute_operation(
        provider=provider,
        operation="edit",
        prompt="edit",
        base_image=base,
        references=(reference,),
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    request = opener.requests[0][0]
    body = json.loads(request.data)
    assert request.full_url == "https://chatgpt.com/backend-api/codex/images/edits"
    assert request.headers["Authorization"] == f"Bearer {access_token}"
    assert request.headers["Chatgpt-account-id"] == account_id
    assert request.headers["Content-type"] == "application/json"
    assert [item["image_url"] for item in body["images"]] == [
        base.data_url,
        reference.data_url,
    ]
    assert "Image 1 is the base image to edit." in body["prompt"]
    assert "Images 2 through 2 are ordered reference images only." in body["prompt"]
    assert "output_format" not in body
    assert "background" not in body
    rendered_report = json.dumps(result.report)
    assert access_token not in rendered_report
    assert account_id not in rendered_report
    assert result.report["protocol_notes"]["ignored_options"] == [
        "background",
        "output_format",
    ]


def test_oauth_generate_responses_applies_codex_image_invariants(monkeypatch):
    from gpt_image_bridge.config import ProviderConfig

    access_token = "oauth-access-token-for-responses-test"
    monkeypatch.setattr(
        "gpt_image_bridge.protocols.oauth_request_context",
        lambda **_kwargs: (
            "https://chatgpt.com/backend-api/codex",
            {
                "Authorization": f"Bearer {access_token}",
                "chatgpt-account-id": "account-for-responses-test",
            },
            (access_token, "account-for-responses-test"),
        ),
    )
    provider = ProviderConfig(
        auth_mode="codex_oauth",
        base_url="https://api.openai.com/v1",
        model="gpt-image-2",
        api_protocol="auto",
    )
    completed = {
        "type": "response.completed",
        "response": {
            "output": [
                {
                    "type": "image_generation_call",
                    "result": png_b64(),
                }
            ]
        },
    }
    opener = QueueOpener(
        FakeResponse(
            f"data: {json.dumps(completed)}\n\n".encode(),
            headers={"content-type": "text/event-stream"},
        )
    )
    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="generate",
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    request = opener.requests[0][0]
    payload = json.loads(request.data)
    assert request.full_url == "https://chatgpt.com/backend-api/codex/responses"
    assert payload["instructions"] == ""
    assert payload["store"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["stream"] is True
    assert "max_output_tokens" not in payload
    assert result.report["selected_protocol"] == "responses"
    assert len(result.images) == 1


def test_node_api_key_is_sent_only_as_authorization(api_provider, image_json):
    opener = QueueOpener(FakeResponse(image_json))
    result = execute_operation(
        provider=api_provider,
        operation="generate",
        prompt="authorization test",
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    request = opener.requests[0][0]
    assert request.headers["Authorization"] == "Bearer " + "test-secret-not-for-network"
    assert "test-secret-not-for-network" not in json.dumps(result.report)


def test_success_request_id_cannot_echo_node_api_key(api_provider, image_json):
    secret = api_provider.api_key
    opener = QueueOpener(
        FakeResponse(
            image_json,
            headers={"content-type": "application/json", "x-request-id": secret},
        )
    )
    result = execute_operation(
        provider=api_provider,
        operation="generate",
        prompt="request id redaction",
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    rendered = json.dumps(result.report)
    assert secret not in rendered
    assert result.report["requests"][0]["request_id"] == "[REDACTED]"


def test_success_report_redacts_an_opaque_api_key_from_provider_fields(
    api_provider,
    image_json,
):
    secret = "opaqueCredentialValue987654321"
    provider = replace(
        api_provider,
        api_key=secret,
        base_url=f"https://example.test/{secret}/v1",
        model=f"model-{secret}",
        use_custom_endpoints=False,
        use_async=False,
    )
    opener = QueueOpener(FakeResponse(image_json))
    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="report redaction",
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    rendered = json.dumps(result.report)
    assert secret not in rendered
    assert "[REDACTED]" in rendered


@pytest.mark.parametrize("reference_count", [1, 3])
def test_base_plus_references_preserves_base_first(api_provider, image_json, reference_count):
    base = tensor_to_png(
        image_tensor(9, 7, 0.1),
        source_slot=1,
        role="base",
    )
    refs = encode_reference_slots(
        *[image_tensor(6 + index, 8 + index, 0.2) for index in range(reference_count)]
    )
    opener = QueueOpener(FakeResponse(image_json))
    result = execute_operation(
        provider=api_provider,
        operation="edit",
        prompt="identity reference only",
        base_image=base,
        references=refs,
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    request_body = opener.requests[0][0].data
    positions = [request_body.index(b"image-1-base.png")]
    positions += [
        request_body.index(f"image-{index}-reference.png".encode())
        for index in range(2, reference_count + 2)
    ]
    assert positions == sorted(positions)
    assert [item["role"] for item in result.report["input"]["roles"]] == [
        "base",
        *(["reference"] * reference_count),
    ]
    assert [
        item["source_slot"] for item in result.report["input"]["roles"]
    ] == list(range(1, reference_count + 2))


def test_responses_json_parsing_with_ordered_references(api_provider):
    refs = encode_reference_slots(image_tensor(7, 5), image_tensor(11, 9))
    response_payload = {
        "output": [
            {
                "type": "image_generation_call",
                "result": png_b64(10, 10),
                "revised_prompt": "json revised",
            }
        ]
    }
    opener = QueueOpener(FakeResponse(json.dumps(response_payload).encode()))
    result = execute_operation(
        provider=api_provider,
        operation="generate",
        prompt="use references",
        references=refs,
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    payload = json.loads(opener.requests[0][0].data)
    content = payload["input"][1]["content"]
    assert [item["type"] for item in content] == ["input_image", "input_image", "input_text"]
    assert "References 1 through 2" in content[-1]["text"]
    assert "Images 1 through 2" not in content[-1]["text"]
    assert result.revised_prompt == "json revised"
    assert result.report["selected_protocol"] == "responses"
    assert [
        item["source_slot"] for item in result.report["input"]["roles"]
    ] == [1, 2]


def test_responses_sse_parsing(api_provider):
    provider = with_protocol(api_provider, "responses")
    encoded = png_b64(12, 6)
    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "image_generation_call",
            "result": encoded,
            "revised_prompt": "sse revised",
        },
    }
    sse = f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode()
    opener = QueueOpener(
        FakeResponse(sse, headers={"content-type": "text/event-stream"})
    )
    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="stream",
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    assert result.revised_prompt == "sse revised"
    assert len(result.images) == 1


def test_responses_sse_uses_event_field_and_collected_output_item(api_provider):
    provider = with_protocol(api_provider, "responses")
    encoded = png_b64(12, 6)
    output_item = {
        "item": {
            "id": "image-call-1",
            "type": "image_generation_call",
            "result": encoded,
            "revised_prompt": "event field revised",
        }
    }
    completed = {"response": {"status": "completed", "output": []}}
    sse = (
        "event: response.output_item.done\n"
        f"data: {json.dumps(output_item)}\n\n"
        "event: response.completed\n"
        f"data: {json.dumps(completed)}\n\n"
    ).encode()
    opener = QueueOpener(
        FakeResponse(sse, headers={"content-type": "text/event-stream"})
    )

    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="event field stream",
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )

    assert result.revised_prompt == "event field revised"
    assert len(result.images) == 1


@pytest.mark.parametrize("event_type", ["response.cancelled", "response.canceled"])
def test_responses_sse_cancelled_is_terminal(api_provider, event_type):
    provider = with_protocol(api_provider, "responses")
    cancelled = {
        "response": {
            "status": "cancelled",
            "error": {
                "message": "request was cancelled",
                "type": "cancelled_error",
                "code": "request_cancelled",
            },
        }
    }
    sse = (
        f"event: {event_type}\n"
        f"data: {json.dumps(cancelled)}\n\n"
    ).encode()
    opener = QueueOpener(
        FakeResponse(sse, headers={"content-type": "text/event-stream"})
    )

    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="cancelled stream",
            client=HttpClient(opener=opener, sleeper=lambda _: None),
        )

    assert captured.value.message == "request was cancelled"
    assert captured.value.error_type == "cancelled_error"
    assert captured.value.error_code == "request_cancelled"
    assert captured.value.retryable is False


def test_responses_sse_terminal_status_is_honored_without_event_name(
    api_provider,
):
    provider = with_protocol(api_provider, "responses")
    failed = {
        "type": "response",
        "response": {
            "status": "failed",
            "error": {
                "message": "response failed",
                "type": "upstream_error",
                "code": "response_failed",
            },
        },
    }
    sse = f"data: {json.dumps(failed)}\n\n".encode()
    opener = QueueOpener(
        FakeResponse(sse, headers={"content-type": "text/event-stream"})
    )

    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="failed stream",
            client=HttpClient(opener=opener, sleeper=lambda _: None),
        )

    assert captured.value.message == "response failed"
    assert captured.value.error_type == "upstream_error"
    assert captured.value.error_code == "response_failed"


def test_images_url_download_validates_mime(api_provider):
    payload = json.dumps({"data": [{"url": "https://cdn.test/image.png"}]}).encode()
    opener = QueueOpener(
        FakeResponse(payload),
        FakeResponse(png_bytes(13, 17), headers={"content-type": "image/png"}),
    )
    result = execute_operation(
        provider=api_provider,
        operation="generate",
        prompt="url",
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    assert result.report["output"]["images"][0]["width"] == 13
    download_request = opener.requests[1][0]
    assert "Authorization" not in download_request.headers


def test_images_url_rejects_non_image_mime(api_provider):
    payload = json.dumps({"data": [{"url": "https://cdn.test/not-image"}]}).encode()
    opener = QueueOpener(
        FakeResponse(payload),
        FakeResponse(b"<html>no</html>", headers={"content-type": "text/html"}),
    )
    with pytest.raises(BridgeError, match="unexpected MIME"):
        execute_operation(
            provider=api_provider,
            operation="generate",
            prompt="url",
            client=HttpClient(opener=opener, sleeper=lambda _: None),
        )


def test_responses_safety_refusal_does_not_retry(api_provider):
    provider = with_protocol(api_provider, "responses")
    payload = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "refusal", "refusal": "policy refusal"}],
            }
        ],
        "moderation_category": "safety",
    }
    opener = QueueOpener(FakeResponse(json.dumps(payload).encode()))
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="refused",
            client=HttpClient(opener=opener, sleeper=lambda _: None),
        )
    assert len(opener.requests) == 1
    assert captured.value.error_type == "safety_refusal"
    assert captured.value.retryable is False
    assert captured.value.status == 200
    assert captured.value.protocol == "responses"


def test_responses_json_provider_echo_cannot_leak_node_api_key(api_provider):
    provider = with_protocol(api_provider, "responses")
    secret = api_provider.api_key
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "refusal",
                        "refusal": f"request rejected after seeing {secret}",
                    }
                ],
            }
        ]
    }
    opener = QueueOpener(FakeResponse(json.dumps(payload).encode()))
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="redaction",
            client=HttpClient(opener=opener, sleeper=lambda _: None),
        )
    rendered = str(captured.value)
    assert secret not in rendered
    assert "[REDACTED]" in rendered


def test_responses_sse_provider_echo_cannot_leak_node_api_key(api_provider):
    provider = with_protocol(api_provider, "responses")
    secret = api_provider.api_key
    event = {
        "type": "error",
        "error": {
            "message": f"upstream echoed {secret}",
            "type": "provider_error",
        },
    }
    opener = QueueOpener(
        FakeResponse(
            f"data: {json.dumps(event)}\n\n".encode(),
            headers={"content-type": "text/event-stream"},
        )
    )
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="redaction",
            client=HttpClient(opener=opener, sleeper=lambda _: None),
        )
    rendered = str(captured.value)
    assert secret not in rendered
    assert "upstream echoed [REDACTED]" in rendered


def test_provider_error_keeps_non_sensitive_diagnostic_text(api_provider):
    provider = with_protocol(api_provider, "responses")
    event = {
        "type": "error",
        "error": {"message": "ordinary provider failure", "type": "provider_error"},
    }
    opener = QueueOpener(
        FakeResponse(
            f"data: {json.dumps(event)}\n\n".encode(),
            headers={"content-type": "text/event-stream"},
        )
    )
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="diagnostic",
            client=HttpClient(opener=opener, sleeper=lambda _: None),
        )
    assert "ordinary provider failure" in str(captured.value)


def test_revised_prompt_cannot_echo_node_api_key(api_provider):
    provider = with_protocol(api_provider, "responses")
    secret = api_provider.api_key
    payload = {
        "output": [
            {
                "type": "image_generation_call",
                "result": png_b64(),
                "revised_prompt": f"safe image {secret}",
            }
        ]
    }
    opener = QueueOpener(FakeResponse(json.dumps(payload).encode()))
    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="redaction",
        client=HttpClient(opener=opener, sleeper=lambda _: None),
    )
    assert secret not in result.revised_prompt
    assert result.revised_prompt == "safe image [REDACTED]"
