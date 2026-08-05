from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Callable

import pytest

import gpt_image_bridge.protocols as protocols
from gpt_image_bridge.errors import BridgeError
from gpt_image_bridge.images import tensor_to_png
from gpt_image_bridge.protocols import execute_operation
from gpt_image_bridge.transport import HttpClient, HttpResponse

from conftest import FakeResponse, QueueOpener, image_tensor, png_b64, png_bytes


@dataclass(frozen=True)
class JsonStep:
    payload: Any
    status: int = 200
    headers: dict[str, str] | None = None


class ScriptedClient:
    def __init__(
        self,
        *steps: JsonStep | BridgeError | Callable[[dict[str, Any]], JsonStep],
        default: JsonStep | Callable[[dict[str, Any]], JsonStep] | None = None,
        clock: FakeClock | None = None,
    ) -> None:
        self.steps = list(steps)
        self.default = default
        self.clock = clock or FakeClock()
        self.calls: list[dict[str, Any]] = []

    def monotonic(self) -> float:
        return self.clock()

    def sleep(self, seconds: float) -> None:
        self.clock.sleep(seconds)

    def request(self, **kwargs: Any) -> HttpResponse:
        self.calls.append(kwargs)
        if self.steps:
            step = self.steps.pop(0)
        elif self.default is not None:
            step = self.default
        else:
            raise AssertionError(f"Unexpected HTTP request: {kwargs}")
        if isinstance(step, BridgeError):
            raise step
        if callable(step):
            step = step(kwargs)
        return HttpResponse(
            status=step.status,
            headers=step.headers or {"content-type": "application/json"},
            body=(
                step.payload
                if isinstance(step.payload, bytes)
                else json.dumps(step.payload).encode()
            ),
            elapsed_ms=1,
            attempts=1,
            url=kwargs["url"],
        )


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += max(0.0, float(seconds))


def _async_provider(api_provider):
    return replace(api_provider, use_async=True)


def _method(call: dict[str, Any]) -> str:
    return str(call.get("method", "POST")).upper()


def _post_calls(client: ScriptedClient) -> list[dict[str, Any]]:
    return [call for call in client.calls if _method(call) == "POST"]


def _disable_real_poll_sleep(monkeypatch) -> None:
    monkeypatch.setattr(protocols.time, "sleep", lambda _seconds: None)


def test_async_generation_polls_one_task_and_parses_success(
    api_provider,
    monkeypatch,
):
    _disable_real_poll_sleep(monkeypatch)
    client = ScriptedClient(
        JsonStep({"task_id": "generate-task", "status": "running"}, status=202),
        JsonStep({"task_id": "generate-task", "status": "queued"}),
        JsonStep(
            {
                "task_id": "generate-task",
                "status": "success",
                "result": {
                    "data": [
                        {
                            "b64_json": png_b64(13, 17),
                            "revised_prompt": "async revised",
                        }
                    ]
                },
            }
        ),
    )

    result = execute_operation(
        provider=_async_provider(api_provider),
        operation="generate",
        prompt="async generation",
        timeout=30,
        client=client,
    )

    assert [call["url"] for call in client.calls] == [
        "https://example.test/v1/images/generations/async",
        "https://example.test/v1/images/tasks/generate-task",
        "https://example.test/v1/images/tasks/generate-task",
    ]
    assert [_method(call) for call in client.calls] == ["POST", "GET", "GET"]
    assert client.calls[0]["max_attempts"] == 1
    assert len(_post_calls(client)) == 1
    assert result.revised_prompt == "async revised"
    assert result.report["output"]["images"][0]["width"] == 13
    assert result.report["output"]["images"][0]["height"] == 17


def test_async_edit_polls_one_task_and_parses_success(api_provider, monkeypatch):
    _disable_real_poll_sleep(monkeypatch)
    client = ScriptedClient(
        JsonStep({"task_id": "edit-task", "status": "running"}, status=202),
        JsonStep(
            {
                "task_id": "edit-task",
                "status": "succeeded",
                "result": {"data": [{"b64_json": png_b64(19, 11)}]},
            }
        ),
    )
    base = tensor_to_png(
        image_tensor(9, 7),
        source_slot=1,
        role="base",
    )

    result = execute_operation(
        provider=_async_provider(api_provider),
        operation="edit",
        prompt="async edit",
        base_image=base,
        timeout=30,
        client=client,
    )

    assert [call["url"] for call in client.calls] == [
        "https://example.test/v1/images/edits/async",
        "https://example.test/v1/images/tasks/edit-task",
    ]
    assert [_method(call) for call in client.calls] == ["POST", "GET"]
    assert client.calls[0]["max_attempts"] == 1
    assert b"image-1-base.png" in client.calls[0]["body"]
    assert len(_post_calls(client)) == 1
    assert result.report["output"]["images"][0]["width"] == 19
    assert result.report["output"]["images"][0]["height"] == 11


@pytest.mark.parametrize("status", [404, 405, 501])
def test_async_capability_rejection_falls_back_to_sync_once(
    api_provider,
    status,
):
    client = ScriptedClient(
        BridgeError(
            "async route unsupported",
            status=status,
            error_code="route_not_found",
        ),
        JsonStep({"data": [{"b64_json": png_b64(7, 5)}]}),
    )

    result = execute_operation(
        provider=_async_provider(api_provider),
        operation="generate",
        prompt="fallback exactly once",
        timeout=30,
        client=client,
    )

    assert [call["url"] for call in client.calls] == [
        "https://example.test/v1/images/generations/async",
        "https://example.test/v1/images/generations",
    ]
    assert [_method(call) for call in client.calls] == ["POST", "POST"]
    assert [call["max_attempts"] for call in client.calls] == [1, 1]
    assert result.report["output"]["count"] == 1


@pytest.mark.parametrize(
    "error",
    [
        BridgeError("unauthorized", status=401, error_code="unauthorized"),
        BridgeError("bad request", status=400, error_code="invalid_request"),
        BridgeError("forbidden", status=403, error_code="forbidden"),
        BridgeError("request timeout", status=408, error_code="timeout"),
        BridgeError(
            "rate limited",
            status=429,
            error_code="rate_limit_exceeded",
            retryable=True,
        ),
        BridgeError(
            "upstream failed",
            status=500,
            error_code="upstream_error",
            retryable=True,
        ),
        BridgeError(
            "temporarily unavailable",
            status=503,
            error_code="unavailable",
            retryable=True,
        ),
        BridgeError(
            "bad gateway",
            status=502,
            error_code="upstream_error",
            retryable=True,
        ),
        BridgeError(
            "gateway timeout",
            status=504,
            error_code="upstream_timeout",
            retryable=True,
        ),
        BridgeError(
            "connection reset",
            error_type="network",
            error_code="network_error",
            retryable=True,
        ),
    ],
    ids=["401", "400", "403", "408", "429", "500", "503", "502", "504", "network"],
)
def test_async_submit_non_capability_errors_never_fall_back(api_provider, error):
    client = ScriptedClient(error)

    with pytest.raises(BridgeError):
        execute_operation(
            provider=_async_provider(api_provider),
            operation="generate",
            prompt="do not duplicate",
            timeout=30,
            client=client,
        )

    assert len(client.calls) == 1
    assert _method(client.calls[0]) == "POST"
    assert client.calls[0]["url"].endswith("/images/generations/async")
    assert client.calls[0]["max_attempts"] == 1


def test_async_terminal_failure_after_task_creation_never_falls_back(
    api_provider,
    monkeypatch,
):
    _disable_real_poll_sleep(monkeypatch)
    client = ScriptedClient(
        JsonStep({"task_id": "charged-task", "status": "running"}, status=202),
        JsonStep(
            {
                "task_id": "charged-task",
                "status": "failed",
                "error": {
                    "message": "provider task failed",
                    "code": "generation_failed",
                },
            }
        ),
    )

    with pytest.raises(BridgeError):
        execute_operation(
            provider=_async_provider(api_provider),
            operation="generate",
            prompt="never resubmit an existing task",
            timeout=30,
            client=client,
        )

    assert [_method(call) for call in client.calls] == ["POST", "GET"]
    assert len(_post_calls(client)) == 1
    assert all(
        not call["url"].endswith("/images/generations")
        for call in client.calls
    )


def test_async_poll_timeout_after_task_creation_never_falls_back(
    api_provider,
    monkeypatch,
):
    clock = FakeClock()
    monkeypatch.setattr(protocols.time, "monotonic", clock)
    monkeypatch.setattr(protocols.time, "sleep", clock.sleep)
    client = ScriptedClient(
        JsonStep({"task_id": "slow-task", "status": "running"}, status=202),
        default=JsonStep({"task_id": "slow-task", "status": "running"}),
        clock=clock,
    )

    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=_async_provider(api_provider),
            operation="generate",
            prompt="timeout without resubmission",
            timeout=5,
            client=client,
        )

    assert captured.value.error_code == "async_poll_timeout"
    assert len(_post_calls(client)) == 1
    assert all(
        not call["url"].endswith("/images/generations")
        for call in client.calls
    )
    assert clock.value <= 5


def test_async_submit_explicitly_disables_transport_retries(api_provider):
    client = ScriptedClient(
        BridgeError(
            "ambiguous submit failure",
            status=500,
            error_code="upstream_error",
            retryable=True,
        )
    )

    with pytest.raises(BridgeError):
        execute_operation(
            provider=_async_provider(api_provider),
            operation="generate",
            prompt="single submit attempt",
            timeout=30,
            client=client,
        )

    assert len(client.calls) == 1
    assert client.calls[0]["max_attempts"] == 1


def test_async_result_url_download_never_receives_authorization(
    api_provider,
    monkeypatch,
):
    _disable_real_poll_sleep(monkeypatch)
    opener = QueueOpener(
        FakeResponse(
            json.dumps({"task_id": "url-task", "status": "running"}).encode(),
            status=202,
        ),
        FakeResponse(
            json.dumps(
                {
                    "task_id": "url-task",
                    "status": "success",
                    "result": {
                        "data": [{"url": "https://cdn.test/async-result.png"}]
                    },
                }
            ).encode()
        ),
        FakeResponse(
            png_bytes(23, 29),
            headers={"content-type": "image/png"},
        ),
    )
    client = HttpClient(opener=opener, sleeper=lambda _seconds: None)

    result = execute_operation(
        provider=_async_provider(api_provider),
        operation="generate",
        prompt="download async result",
        timeout=30,
        client=client,
    )

    requests = [request for request, _timeout in opener.requests]
    assert [request.get_method() for request in requests] == ["POST", "GET", "GET"]
    assert requests[0].full_url.endswith("/images/generations/async")
    assert requests[1].full_url.endswith("/images/tasks/url-task")
    assert requests[2].full_url == "https://cdn.test/async-result.png"
    assert "Authorization" in requests[0].headers
    assert "Authorization" in requests[1].headers
    assert "Authorization" not in requests[2].headers
    assert result.report["output"]["images"][0]["width"] == 23
    assert result.report["output"]["images"][0]["height"] == 29


def test_async_submit_can_return_an_immediate_images_result(api_provider):
    client = ScriptedClient(
        JsonStep({"data": [{"b64_json": png_b64(9, 15)}]}),
    )
    result = execute_operation(
        provider=_async_provider(api_provider),
        operation="generate",
        prompt="immediate async response",
        timeout=30,
        client=client,
    )
    assert len(client.calls) == 1
    assert client.calls[0]["url"].endswith("/images/generations/async")
    assert result.report["async"]["effective_mode"] == "async"
    assert result.report["async"]["poll_count"] == 0
    assert result.report["output"]["images"][0]["height"] == 15


def test_async_immediate_task_result_hides_task_id_from_success_report(api_provider):
    task_id = "immediate/private-task"
    client = ScriptedClient(
        JsonStep(
            {
                "task_id": task_id,
                "status": "success",
                "result": {
                    "data": [
                        {
                            "b64_json": png_b64(),
                            "revised_prompt": f"completed {task_id}",
                        }
                    ]
                },
            },
            headers={
                "content-type": "application/json",
                "x-request-id": "immediate%2fprivate-task",
            },
        ),
    )
    result = execute_operation(
        provider=_async_provider(api_provider),
        operation="generate",
        prompt="immediate task result",
        timeout=30,
        client=client,
    )
    rendered = json.dumps(result.report)
    assert task_id not in rendered
    assert "immediate%2Fprivate-task" not in rendered
    assert "immediate%2fprivate-task" not in rendered
    assert "{task_id}" in rendered
    assert result.report["async"]["task_id"]["present"] is True
    assert task_id not in result.revised_prompt
    assert "{task_id}" in result.revised_prompt


def test_async_submit_malformed_success_never_falls_back(api_provider):
    client = ScriptedClient(JsonStep({"status": "running"}))
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=_async_provider(api_provider),
            operation="generate",
            prompt="missing task id",
            timeout=30,
            client=client,
        )
    assert captured.value.error_code == "invalid_async_submit_response"
    assert captured.value.safe_to_resubmit is False
    assert captured.value.remote_task_may_continue is True
    assert len(_post_calls(client)) == 1


def test_unknown_custom_images_path_is_not_guessed_as_async(api_provider):
    provider = replace(
        api_provider,
        api_protocol="images",
        use_custom_endpoints=True,
        use_async=True,
        generate_endpoint="vendor/generate-image",
    )
    client = ScriptedClient(
        JsonStep({"data": [{"b64_json": png_b64()}]}),
    )
    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="custom sync route",
        timeout=30,
        client=client,
    )
    assert [call["url"] for call in client.calls] == [
        "https://example.test/v1/vendor/generate-image"
    ]
    assert result.report["async"]["attempted"] is False
    assert result.report["async"]["effective_mode"] == "sync"
    assert "not a standard Images" in result.report["async"]["skipped_reason"]


def test_custom_async_generation_supports_query_polling_and_json_mapping(
    api_provider,
):
    task_id = "vendor/job 42"
    provider = replace(
        api_provider,
        api_protocol="auto",
        use_custom_endpoints=True,
        use_async=True,
        generate_endpoint="vendor/render",
        async_generate_endpoint="queue/create-image",
        async_poll_endpoint_template="queue/status?job={task_id}",
        async_mapping_json=json.dumps(
            {
                "task_id_paths": ["/job/id"],
                "status_paths": ["/job/state"],
                "result_paths": ["/job/output"],
                "image_items_paths": ["/pictures"],
                "image_b64_paths": ["/encoded"],
                "revised_prompt_paths": ["/caption"],
                "success_statuses": ["DONE"],
                "failure_statuses": ["ERROR"],
                "error_message_paths": ["/job/message"],
            }
        ),
    )
    client = ScriptedClient(
        JsonStep(
            {"job": {"id": task_id, "state": "waiting"}},
            status=202,
        ),
        JsonStep(
            {
                "job": {
                    "state": "done",
                    "output": {
                        "pictures": [
                            {
                                "encoded": png_b64(31, 23),
                                "caption": "mapped result",
                            }
                        ]
                    },
                }
            }
        ),
    )

    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="custom asynchronous generation",
        timeout=30,
        client=client,
    )

    assert [call["url"] for call in client.calls] == [
        "https://example.test/v1/queue/create-image",
        "https://example.test/v1/queue/status?job=vendor%2Fjob%2042",
    ]
    assert [_method(call) for call in client.calls] == ["POST", "GET"]
    assert len(_post_calls(client)) == 1
    assert result.revised_prompt == "mapped result"
    assert result.report["output"]["images"][0]["width"] == 31
    assert result.report["output"]["images"][0]["height"] == 23
    assert result.report["async"]["contract"] == "custom"
    assert result.report["async"]["mapping_mode"] == "custom"
    rendered = json.dumps(result.report)
    assert task_id not in rendered
    assert "{task_id}" in rendered


def test_json_pointer_mapping_supports_fallback_escape_and_array_index():
    payload = {
        "job/meta": {"~id": "mapped-task"},
        "events": [{"state": "running"}],
    }
    assert protocols._first_mapped_value(
        payload,
        ("/missing", "/job~1meta/~0id"),
    ) == "mapped-task"
    assert protocols._json_pointer_value(payload, "/events/0/state") == "running"


def test_custom_async_edit_supports_path_polling_and_a_different_contract(
    api_provider,
):
    provider = replace(
        api_provider,
        api_protocol="images",
        use_custom_endpoints=True,
        use_async=True,
        edit_endpoint="vendor/edit",
        async_edit_endpoint="tasks/edit/submit",
        async_poll_endpoint_template="tasks/{task_id}/result",
        async_mapping_json=json.dumps(
            {
                "task_id_paths": ["/id"],
                "status_paths": ["/stage"],
                "result_paths": ["/asset"],
                "image_b64_paths": ["/content"],
                "success_statuses": ["READY"],
                "failure_statuses": ["REJECTED"],
            }
        ),
    )
    client = ScriptedClient(
        JsonStep({"id": "edit:42", "stage": "processing"}, status=202),
        JsonStep(
            {
                "stage": "ready",
                "asset": {"content": png_b64(17, 21)},
            }
        ),
    )
    base = tensor_to_png(image_tensor(11, 9), source_slot=1, role="base")

    result = execute_operation(
        provider=provider,
        operation="edit",
        prompt="custom asynchronous edit",
        base_image=base,
        timeout=30,
        client=client,
    )

    assert [call["url"] for call in client.calls] == [
        "https://example.test/v1/tasks/edit/submit",
        "https://example.test/v1/tasks/edit%3A42/result",
    ]
    assert [_method(call) for call in client.calls] == ["POST", "GET"]
    assert b"image-1-base.png" in client.calls[0]["body"]
    assert len(_post_calls(client)) == 1
    assert result.report["output"]["images"][0]["width"] == 17
    assert result.report["output"]["images"][0]["height"] == 21


def test_custom_async_url_mapping_does_not_treat_a_url_as_base64(api_provider):
    provider = replace(
        api_provider,
        api_protocol="images",
        use_custom_endpoints=True,
        use_async=True,
        generate_endpoint="vendor/render",
        async_generate_endpoint="queue/create",
        async_poll_endpoint_template="queue/{task_id}",
        async_mapping_json=json.dumps(
            {
                "task_id_paths": ["/id"],
                "status_paths": ["/state"],
                "result_paths": ["/output"],
                "image_items_paths": ["/files"],
                "image_url_paths": [""],
                "success_statuses": ["done"],
            }
        ),
    )
    client = ScriptedClient(
        JsonStep({"id": "url-task", "state": "running"}, status=202),
        JsonStep(
            {
                "state": "done",
                "output": {"files": ["https://cdn.test/custom-result.png"]},
            }
        ),
        JsonStep(
            png_bytes(25, 15),
            headers={"content-type": "image/png"},
        ),
    )

    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="mapped URL result",
        timeout=30,
        client=client,
    )

    assert [_method(call) for call in client.calls] == ["POST", "GET", "GET"]
    assert client.calls[-1]["url"] == "https://cdn.test/custom-result.png"
    assert result.report["output"]["images"][0]["width"] == 25


def test_custom_async_mapping_failure_after_submit_never_resubmits(
    api_provider,
):
    provider = replace(
        api_provider,
        api_protocol="images",
        use_custom_endpoints=True,
        use_async=True,
        generate_endpoint="vendor/render",
        async_generate_endpoint="queue/create",
        async_poll_endpoint_template="queue/{task_id}",
        async_mapping_json='{"task_id_paths":["/job"]}',
    )
    client = ScriptedClient(
        JsonStep({"job": {"id": "already-created"}}, status=202),
    )

    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="mapping error must not duplicate",
            timeout=30,
            client=client,
        )

    assert captured.value.error_code == "invalid_async_mapping_value"
    assert captured.value.phase == "async_submit"
    assert captured.value.safe_to_resubmit is False
    assert captured.value.remote_task_may_continue is True
    assert len(_post_calls(client)) == 1


def test_submit_mapping_error_hides_an_already_parsed_task_id(api_provider):
    task_id = "private/submit-task"
    provider = replace(
        api_provider,
        api_protocol="images",
        use_custom_endpoints=True,
        use_async=True,
        generate_endpoint="vendor/render",
        async_generate_endpoint="queue/create",
        async_poll_endpoint_template="queue/{task_id}",
        async_mapping_json=json.dumps(
            {
                "task_id_paths": ["/id"],
                "status_paths": ["/state"],
            }
        ),
    )
    client = ScriptedClient(
        JsonStep(
            {"id": task_id, "state": {"unexpected": True}},
            status=202,
            headers={
                "content-type": "application/json",
                "x-request-id": task_id,
            },
        )
    )

    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="submit mapping failure",
            timeout=30,
            client=client,
        )

    assert captured.value.phase == "async_submit"
    assert captured.value.safe_to_resubmit is False
    assert captured.value.remote_task_may_continue is True
    assert task_id not in str(captured.value)
    assert captured.value.request_id == "{task_id}"
    assert len(_post_calls(client)) == 1


@pytest.mark.parametrize(
    "poll_payload",
    [
        {"state": {"unexpected": True}},
        {
            "state": "done",
            "output": {"content": {"unexpected": True}},
        },
    ],
    ids=["status_mapping", "result_mapping"],
)
def test_poll_mapping_error_hides_task_id_and_never_resubmits(
    api_provider,
    poll_payload,
):
    task_id = "private/poll-task"
    provider = replace(
        api_provider,
        api_protocol="images",
        use_custom_endpoints=True,
        use_async=True,
        generate_endpoint="vendor/render",
        async_generate_endpoint="queue/create",
        async_poll_endpoint_template="queue/{task_id}",
        async_mapping_json=json.dumps(
            {
                "task_id_paths": ["/id"],
                "status_paths": ["/state"],
                "result_paths": ["/output"],
                "image_b64_paths": ["/content"],
                "success_statuses": ["done"],
            }
        ),
    )
    client = ScriptedClient(
        JsonStep({"id": task_id, "state": "running"}, status=202),
        JsonStep(
            poll_payload,
            headers={
                "content-type": "application/json",
                "x-request-id": task_id,
            },
        ),
    )

    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="poll mapping failure",
            timeout=30,
            client=client,
        )

    assert captured.value.error_code == "invalid_async_mapping_value"
    assert captured.value.phase == "async_poll"
    assert captured.value.safe_to_resubmit is False
    assert captured.value.remote_task_may_continue is True
    assert task_id not in str(captured.value)
    assert captured.value.request_id == "{task_id}"
    assert [_method(call) for call in client.calls] == ["POST", "GET"]
    assert len(_post_calls(client)) == 1


def test_explicit_submit_failure_cannot_be_overridden_by_image_data(api_provider):
    client = ScriptedClient(
        JsonStep(
            {
                "status": "failed",
                "message": "provider rejected the task",
                "data": [{"b64_json": png_b64()}],
            }
        )
    )

    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=_async_provider(api_provider),
            operation="generate",
            prompt="failure status wins",
            timeout=30,
            client=client,
        )

    assert "provider rejected" in captured.value.message
    assert len(_post_calls(client)) == 1


def test_async_failure_diagnostics_are_redacted_and_length_bounded(api_provider):
    secret = "opaqueCredentialValue987654321"
    provider = replace(api_provider, api_key=secret, use_async=True)
    client = ScriptedClient(
        JsonStep(
            {
                "status": "failed",
                "message": secret + ("x" * 5000),
                "error": {"type": "t" * 1000, "code": "c" * 1000},
            }
        )
    )

    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="bounded diagnostics",
            timeout=30,
            client=client,
        )

    rendered = str(captured.value)
    assert secret not in rendered
    assert "[REDACTED]" in rendered
    assert "[truncated]" in rendered
    assert len(captured.value.message) < 2200
    assert len(captured.value.error_type or "") < 400
    assert len(captured.value.error_code or "") < 400


def test_custom_async_capability_rejection_falls_back_to_custom_sync_once(
    api_provider,
):
    provider = replace(
        api_provider,
        api_protocol="images",
        use_custom_endpoints=True,
        use_async=True,
        generate_endpoint="vendor/render-sync",
        async_generate_endpoint="vendor/render-async",
        async_poll_endpoint_template="vendor/jobs/{task_id}",
    )
    client = ScriptedClient(
        BridgeError("custom async route unsupported", status=405),
        JsonStep({"data": [{"b64_json": png_b64(9, 7)}]}),
    )

    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="custom fallback",
        timeout=30,
        client=client,
    )

    assert [call["url"] for call in client.calls] == [
        "https://example.test/v1/vendor/render-async",
        "https://example.test/v1/vendor/render-sync",
    ]
    assert [_method(call) for call in client.calls] == ["POST", "POST"]
    assert result.report["async"]["effective_mode"] == "sync_fallback"


def test_custom_async_route_preflight_failure_sends_no_request(api_provider):
    provider = replace(
        api_provider,
        api_protocol="images",
        use_custom_endpoints=True,
        use_async=True,
        generate_endpoint="vendor/render",
        async_generate_endpoint="queue/create",
    )
    client = ScriptedClient()

    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="missing poll route",
            timeout=30,
            client=client,
        )

    assert captured.value.error_code == "custom_async_poll_endpoint_missing"
    assert captured.value.safe_to_resubmit is True
    assert client.calls == []


@pytest.mark.parametrize("use_async", [False, True])
def test_custom_endpoint_field_rejects_an_async_route_before_network(
    api_provider,
    use_async,
):
    provider = replace(
        api_provider,
        api_protocol="images",
        use_custom_endpoints=True,
        use_async=use_async,
        edit_endpoint="images/edits/async",
    )
    client = ScriptedClient()
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="edit",
            prompt="do not append async twice",
            base_image=tensor_to_png(image_tensor(8, 8), role="base"),
            timeout=30,
            client=client,
        )
    assert captured.value.error_code == "async_endpoint_must_be_sync_route"
    assert client.calls == []


def test_async_task_url_preserves_query_and_encodes_one_path_segment(api_provider):
    task_id = "task/with?reserved#characters"
    provider = replace(
        api_provider,
        api_protocol="images",
        use_custom_endpoints=True,
        use_async=True,
        generate_endpoint="images/generations?api-version=2026-01-01",
    )
    client = ScriptedClient(
        JsonStep({"task_id": task_id, "status": "running"}, status=202),
        JsonStep(
            {
                "task_id": task_id,
                "status": "success",
                "result": {"data": [{"b64_json": png_b64()}]},
            }
        ),
    )
    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="encoded task path",
        timeout=30,
        client=client,
    )
    assert client.calls[0]["url"] == (
        "https://example.test/v1/images/generations/async"
        "?api-version=2026-01-01"
    )
    assert client.calls[1]["url"] == (
        "https://example.test/v1/images/tasks/"
        "task%2Fwith%3Freserved%23characters?api-version=2026-01-01"
    )
    rendered = json.dumps(result.report)
    assert task_id not in rendered
    assert "{task_id}" in rendered


def test_async_poll_transient_error_reuses_the_same_task_without_posting(
    api_provider,
):
    client = ScriptedClient(
        JsonStep({"task_id": "one-task", "status": "running"}, status=202),
        BridgeError(
            "temporary poll failure",
            status=503,
            retryable=True,
        ),
        JsonStep(
            {
                "task_id": "one-task",
                "status": "completed",
                "result": {"data": [{"b64_json": png_b64()}]},
            }
        ),
    )
    result = execute_operation(
        provider=_async_provider(api_provider),
        operation="generate",
        prompt="retry polling only",
        timeout=30,
        client=client,
    )
    assert [_method(call) for call in client.calls] == ["POST", "GET", "GET"]
    assert client.calls[1]["url"] == client.calls[2]["url"]
    assert len(_post_calls(client)) == 1
    assert result.report["async"]["transient_poll_errors"] == 1


def test_non_images_protocol_reports_that_async_was_not_attempted(api_provider):
    provider = replace(
        api_provider,
        api_protocol="responses",
        use_async=True,
    )
    response_payload = {
        "output": [
            {
                "type": "image_generation_call",
                "result": png_b64(),
            }
        ]
    }
    client = ScriptedClient(JsonStep(response_payload))
    result = execute_operation(
        provider=provider,
        operation="generate",
        prompt="responses remains synchronous",
        timeout=30,
        client=client,
    )
    assert client.calls[0]["url"].endswith("/responses")
    assert result.report["async"]["requested"] is True
    assert result.report["async"]["attempted"] is False
    assert result.report["async"]["effective_mode"] == "sync"


def test_cancellation_after_async_rejection_prevents_sync_fallback(
    api_provider,
    monkeypatch,
):
    class Cancelled(RuntimeError):
        pass

    checks = 0

    def interrupt_on_fallback():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise Cancelled("cancelled")

    monkeypatch.setattr(protocols, "_check_comfy_interrupted", interrupt_on_fallback)
    client = ScriptedClient(BridgeError("not found", status=404))
    with pytest.raises(Cancelled):
        execute_operation(
            provider=_async_provider(api_provider),
            operation="generate",
            prompt="cancel before fallback",
            timeout=30,
            client=client,
        )
    assert len(_post_calls(client)) == 1


def test_elapsed_deadline_after_async_rejection_prevents_sync_fallback(api_provider):
    clock = FakeClock()

    def delayed_rejection(_call):
        clock.sleep(5)
        raise BridgeError("not found after deadline", status=404)

    client = ScriptedClient(delayed_rejection, clock=clock)
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=_async_provider(api_provider),
            operation="generate",
            prompt="deadline before fallback",
            timeout=5,
            client=client,
        )
    assert captured.value.error_code == "async_operation_timeout"
    assert len(_post_calls(client)) == 1


def test_sync_fallback_parse_failure_is_marked_unsafe_to_resubmit(api_provider):
    client = ScriptedClient(
        BridgeError("not found", status=404),
        JsonStep({"data": []}),
    )
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=_async_provider(api_provider),
            operation="generate",
            prompt="fallback response has no image",
            timeout=30,
            client=client,
        )
    assert captured.value.phase == "sync_fallback"
    assert captured.value.safe_to_resubmit is False
    assert captured.value.remote_task_may_continue is False
    assert len(_post_calls(client)) == 2


def test_terminal_task_failure_hides_task_id_from_every_error_field(api_provider):
    task_id = "private/task?id"
    client = ScriptedClient(
        JsonStep({"task_id": task_id, "status": "running"}, status=202),
        JsonStep(
            {
                "task_id": task_id,
                "status": "failed",
                "error": {
                    "message": f"task {task_id} failed",
                    "type": task_id,
                    "code": task_id,
                },
            },
            headers={
                "content-type": "application/json",
                "x-request-id": task_id,
            },
        ),
    )
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=_async_provider(api_provider),
            operation="generate",
            prompt="private task id",
            timeout=30,
            client=client,
        )
    assert task_id not in str(captured.value)
    assert "private%2Ftask%3Fid" not in str(captured.value)
    assert "{task_id}" in str(captured.value)


def test_async_result_metadata_cannot_echo_task_id(api_provider):
    task_id = "private-task-identifier"
    client = ScriptedClient(
        JsonStep({"task_id": task_id, "status": "running"}, status=202),
        JsonStep(
            {
                "task_id": task_id,
                "status": "success",
                "result": {
                    "data": [{"url": "https://cdn.test/result.png"}],
                },
            }
        ),
        JsonStep(
            png_bytes(7, 9),
            headers={"content-type": f"image/{task_id}"},
        ),
    )
    result = execute_operation(
        provider=_async_provider(api_provider),
        operation="generate",
        prompt="metadata task id redaction",
        timeout=30,
        client=client,
    )
    rendered = json.dumps(result.report)
    assert task_id not in rendered
    assert result.report["output"]["images"][0]["mime_type"] == "image/{task_id}"


def test_async_preflight_error_cannot_leak_opaque_api_key(api_provider):
    secret = "opaqueCredentialValue987654321"
    provider = replace(
        api_provider,
        api_key=secret,
        use_custom_endpoints=True,
        use_async=True,
        generate_endpoint=f"/{secret}/images/generations/async",
    )
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="preflight redaction",
            timeout=30,
            client=ScriptedClient(),
        )
    assert secret not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)


def test_invalid_task_id_cannot_echo_through_request_id(api_provider):
    candidate = "invalid\ntask-id"
    client = ScriptedClient(
        JsonStep(
            {"task_id": candidate, "status": "running"},
            status=202,
            headers={
                "content-type": "application/json",
                "x-request-id": candidate,
            },
        ),
    )
    with pytest.raises(BridgeError) as captured:
        execute_operation(
            provider=_async_provider(api_provider),
            operation="generate",
            prompt="invalid task id",
            timeout=30,
            client=client,
        )
    rendered = str(captured.value)
    assert candidate not in rendered
    assert captured.value.request_id == "[REDACTED]"
    assert captured.value.error_code == "invalid_async_task_id"


def test_sync_fallback_result_download_uses_remaining_shared_deadline(api_provider):
    clock = FakeClock()

    def async_rejection(_call):
        clock.sleep(1)
        raise BridgeError("async route missing", status=404)

    def sync_response(_call):
        clock.sleep(20)
        return JsonStep({"data": [{"url": "https://cdn.test/fallback.png"}]})

    def download_response(call):
        assert call["timeout"] == 9
        assert call["max_attempts"] == 1
        return JsonStep(
            png_bytes(7, 9),
            headers={"content-type": "image/png"},
        )

    client = ScriptedClient(
        async_rejection,
        sync_response,
        download_response,
        clock=clock,
    )
    result = execute_operation(
        provider=_async_provider(api_provider),
        operation="generate",
        prompt="shared fallback deadline",
        timeout=30,
        client=client,
    )
    assert result.report["async"]["effective_mode"] == "sync_fallback"
    assert [_method(call) for call in client.calls] == ["POST", "POST", "GET"]


@pytest.mark.parametrize("protocol", ["images", "responses", "chat_completions"])
def test_paid_image_post_is_never_automatically_retried(api_provider, protocol):
    provider = replace(
        api_provider,
        api_protocol=protocol,
        use_async=False,
    )
    client = ScriptedClient(
        BridgeError(
            "ambiguous paid POST failure",
            status=503,
            retryable=True,
        )
    )
    with pytest.raises(BridgeError):
        execute_operation(
            provider=provider,
            operation="generate",
            prompt="one paid POST only",
            timeout=30,
            client=client,
        )
    assert len(client.calls) == 1
    assert client.calls[0]["max_attempts"] == 1
