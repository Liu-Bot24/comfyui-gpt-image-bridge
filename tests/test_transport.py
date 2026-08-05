from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from email.message import Message

import pytest

from gpt_image_bridge.errors import BridgeError
from gpt_image_bridge.transport import HttpClient, _SafeRedirectHandler

from conftest import FakeResponse, QueueOpener


def http_error(status, payload, headers=None):
    return urllib.error.HTTPError(
        "https://example.test/v1/images/generations",
        status,
        "error",
        headers or {"content-type": "application/json"},
        io.BytesIO(json.dumps(payload).encode()),
    )


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_http_statuses_retry(status):
    opener = QueueOpener(
        http_error(status, {"error": {"message": "retry"}}),
        FakeResponse(b"{}"),
    )
    sleeps = []
    response = HttpClient(opener=opener, sleeper=sleeps.append).request(
        url="https://example.test/v1/images/generations",
        body=b"{}",
        max_attempts=3,
    )
    assert response.attempts == 2
    assert len(opener.requests) == 2
    assert sleeps


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_non_retryable_http_statuses_do_not_retry(status):
    opener = QueueOpener(
        http_error(
            status,
            {
                "error": {
                    "message": "rejected",
                    "type": "authentication" if status in (401, 403) else "invalid_request",
                    "code": "denied",
                    "category": "policy" if status == 400 else None,
                }
            },
            {"content-type": "application/json", "x-request-id": "req_test"},
        )
    )
    with pytest.raises(BridgeError) as captured:
        HttpClient(opener=opener, sleeper=lambda _: None).request(
            url="https://example.test/v1/images/generations",
            body=b"{}",
        )
    assert len(opener.requests) == 1
    assert captured.value.status == status
    assert captured.value.request_id == "req_test"
    assert captured.value.retryable is False
    assert captured.value.__cause__ is None


def test_network_error_retries_without_leaking_secret():
    secret = "sk" + "-secret-value-never-print"
    opener = QueueOpener(
        urllib.error.URLError(f"connection reset near {secret}"),
        urllib.error.URLError("still unavailable"),
    )
    with pytest.raises(BridgeError) as captured:
        HttpClient(opener=opener, sleeper=lambda _: None).request(
            url="https://example.test/v1/responses",
            headers={"Authorization": f"Bearer {secret}"},
            known_secrets=(secret,),
            max_attempts=2,
        )
    assert len(opener.requests) == 2
    assert secret not in str(captured.value)
    assert captured.value.__cause__ is None


def test_transport_redacts_bridge_errors_raised_by_lower_layers():
    secret = "opaqueCredentialValue987654321"
    opener = QueueOpener(
        BridgeError(
            f"redirect contained {secret}",
            endpoint=f"https://example.test/{secret}/result",
        )
    )
    with pytest.raises(BridgeError) as captured:
        HttpClient(opener=opener, sleeper=lambda _: None).request(
            url="https://example.test/v1/images/generations",
            known_secrets=(secret,),
            max_attempts=1,
        )
    rendered = str(captured.value)
    assert secret not in rendered
    assert "[REDACTED]" in rendered


def test_all_http_error_fields_and_headers_redact_known_secret():
    secret = "plain-secret-without-key-prefix"
    opener = QueueOpener(
        http_error(
            400,
            {
                "error": {
                    "message": f"message {secret}",
                    "type": f"type-{secret}",
                    "code": f"code-{secret}",
                    "category": f"category-{secret}",
                }
            },
            {
                "content-type": "application/json",
                "x-request-id": f"request-{secret}",
                "retry-after": secret,
            },
        )
    )
    with pytest.raises(BridgeError) as captured:
        HttpClient(opener=opener, sleeper=lambda _: None).request(
            url="https://example.test/v1/images/generations",
            body=b"{}",
            known_secrets=(secret,),
        )
    rendered = str(captured.value)
    assert secret not in rendered
    assert rendered.count("[REDACTED]") >= 5


def test_authenticated_redirect_is_refused_without_exposing_authorization():
    secret = "redirect-secret-value"
    request = urllib.request.Request(
        "https://provider.example/v1/chat/completions",
        data=b"{}",
        headers={"Authorization": f"Bearer {secret}"},
        method="POST",
    )
    with pytest.raises(BridgeError) as captured:
        _SafeRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            Message(),
            "https://other.example/collect",
        )
    assert captured.value.error_code == "authenticated_redirect_refused"
    assert secret not in str(captured.value)


def test_unauthenticated_post_redirect_is_also_refused():
    request = urllib.request.Request(
        "http://127.0.0.1:8765/v1/images/edits",
        data=b"private image body",
        method="POST",
    )
    with pytest.raises(BridgeError) as captured:
        _SafeRedirectHandler().redirect_request(
            request,
            None,
            307,
            "Temporary Redirect",
            Message(),
            "https://other.example/collect",
        )
    assert captured.value.error_code == "request_redirect_refused"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private.png",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost/image.png",
        "http://[::1]/image.png",
    ],
)
def test_output_download_blocks_local_and_metadata_urls(url):
    opener = QueueOpener(FakeResponse(b"not reached"))
    with pytest.raises(BridgeError) as captured:
        HttpClient(opener=opener, sleeper=lambda _: None).request(
            url=url,
            method="GET",
            public_url_only=True,
        )
    assert captured.value.error_code == "unsafe_image_url"
    assert opener.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:bad/image.png",
        "https://[not-an-ipv6]/image.png",
    ],
)
def test_output_download_rejects_malformed_urls(url):
    opener = QueueOpener(FakeResponse(b"not reached"))
    with pytest.raises(BridgeError) as captured:
        HttpClient(opener=opener, sleeper=lambda _: None).request(
            url=url,
            method="GET",
            public_url_only=True,
        )
    assert captured.value.error_code == "invalid_image_url"
    assert opener.requests == []


def test_diagnostic_endpoint_masks_all_query_values():
    opener = QueueOpener(
        http_error(400, {"error": {"message": "bad request"}})
    )
    with pytest.raises(BridgeError) as captured:
        HttpClient(opener=opener, sleeper=lambda _: None).request(
            url=(
                "https://example.test/v1/chat/completions"
                "?X-Amz-Signature=signed-value&api-version=2026-01-01"
            ),
            body=b"{}",
        )
    endpoint = captured.value.endpoint or ""
    assert "signed-value" not in endpoint
    assert "2026-01-01" not in endpoint
    assert "X-Amz-Signature=[REDACTED]" in endpoint
    assert "api-version=[REDACTED]" in endpoint


def test_network_error_message_masks_signed_url_query_values():
    opener = QueueOpener(
        urllib.error.URLError(
            "download failed at "
            "https://cdn.example/image.png?X-Amz-Signature=signed-value&token=opaque"
        )
    )
    with pytest.raises(BridgeError) as captured:
        HttpClient(opener=opener, sleeper=lambda _: None).request(
            url="https://cdn.example/image.png",
            method="GET",
            max_attempts=1,
            public_url_only=True,
        )
    rendered = str(captured.value)
    assert "signed-value" not in rendered
    assert "opaque" not in rendered
    assert "X-Amz-Signature=[REDACTED]" in rendered
    assert "token=[REDACTED]" in rendered
