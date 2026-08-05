from __future__ import annotations

import json

from gpt_image_bridge.errors import BridgeError, report_json, sanitize


def test_exception_and_report_redaction():
    secret = "sk" + "-super-secret-value-123456"
    error = BridgeError(
        f"upstream repeated Bearer {secret} and {secret}",
        endpoint=f"https://example.test/?token={secret}",
    )
    rendered = json.dumps(error.report((secret,)))
    assert secret not in rendered
    assert "Bearer [REDACTED]" in rendered


def test_nested_request_body_and_base64_are_redacted():
    secret = "key-long-secret-value-123456"
    payload = {
        "Authorization": f"Bearer {secret}",
        "nested": {"api_key": secret},
        "image": "data:image/png;base64," + ("A" * 500),
    }
    rendered = report_json(payload, (secret,))
    assert secret not in rendered
    assert "A" * 100 not in rendered
    assert "[REDACTED]" in rendered


def test_positive_and_negative_key_name_redaction():
    cleaned = sanitize({"token": "secret", "token_count": 12, "model": "tokenizer"})
    assert cleaned["token"] == "[REDACTED]"
    assert cleaned["token_count"] == 12
    assert cleaned["model"] == "tokenizer"
