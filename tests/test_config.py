from __future__ import annotations

import pytest

from gpt_image_bridge.config import (
    DEFAULT_API_BASE_URL,
    ProviderConfig,
    configured_endpoint_url,
    endpoint_url,
    normalize_endpoint_override,
    normalize_base_url,
    resolve_api_credential,
)
from gpt_image_bridge.errors import BridgeError
from gpt_image_bridge.session_credentials import (
    clear_session_credentials,
    issue_session_credential,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.openai.com", "https://api.openai.com/v1"),
        ("https://api.openai.com/", "https://api.openai.com/v1"),
        ("https://provider.example/v1", "https://provider.example/v1"),
        ("https://provider.example/v1/", "https://provider.example/v1"),
        ("https://provider.example/v1/v1", "https://provider.example/v1"),
        ("https://host.test/openai/v1/", "https://host.test/openai/v1"),
    ],
)
def test_base_url_normalization(raw, expected):
    assert normalize_base_url(raw) == expected
    assert endpoint_url(raw, "/images/edits") == f"{expected}/images/edits"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ftp://host/v1",
        "not-a-url",
        "https://x/v1?key=x",
        "https://user@host/v1",
        "https://user:password@host/v1",
    ],
)
def test_invalid_or_empty_base_url_fails(raw):
    with pytest.raises(ValueError):
        normalize_base_url(raw)


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ("chat/completions", "chat/completions"),
        ("/v1/chat/completions", "v1/chat/completions"),
        (
            "https://provider.example/v1/custom/edit",
            "https://provider.example/v1/custom/edit",
        ),
        (
            "chat/completions?api-version=2026-01-01",
            "chat/completions?api-version=2026-01-01",
        ),
    ],
)
def test_endpoint_override_normalization(override, expected):
    assert normalize_endpoint_override(override) == expected


def test_configured_endpoint_accepts_manual_relative_or_same_origin_url():
    base = "https://provider.example/v1"
    assert configured_endpoint_url(base, "", "images/edits") == (
        "https://provider.example/v1/images/edits"
    )
    assert configured_endpoint_url(base, "chat/completions", "images/edits") == (
        "https://provider.example/v1/chat/completions"
    )
    assert configured_endpoint_url(base, "/v1/chat/completions", "images/edits") == (
        "https://provider.example/v1/chat/completions"
    )
    assert configured_endpoint_url(
        base,
        "https://provider.example/v1/vendor/image-edit",
        "images/edits",
    ) == "https://provider.example/v1/vendor/image-edit"
    assert configured_endpoint_url(
        base,
        "https://provider.example:443/v1/vendor/image-edit",
        "images/edits",
    ) == "https://provider.example:443/v1/vendor/image-edit"
    assert configured_endpoint_url(
        base,
        "chat//completions/?api-version=2026-01-01",
        "images/edits",
    ) == (
        "https://provider.example/v1/chat//completions/?api-version=2026-01-01"
    )


@pytest.mark.parametrize(
    "override",
    [
        "https://other.example/v1/chat/completions",
        "chat/completions?token=x",
        "../chat/completions",
        r"chat\completions",
    ],
)
def test_unsafe_endpoint_override_fails(override):
    with pytest.raises(ValueError):
        configured_endpoint_url(
            "https://provider.example/v1",
            override,
            "images/edits",
        )


def test_missing_node_api_key_is_clear_and_does_not_leak():
    provider = ProviderConfig(
        auth_mode="api_key",
        base_url="https://example.test/v1",
        model="gpt-image",
        api_protocol="images",
        api_key="",
    )
    with pytest.raises(BridgeError) as captured:
        resolve_api_credential(provider)
    report = captured.value.report()
    assert report["error_code"] == "credential_missing"
    assert "enter it" in report["message"].lower()
    assert "authorization" not in str(captured.value).lower()
    assert "Bearer" not in str(captured.value)


def test_api_key_is_used_but_never_serialized_or_represented():
    secret = "test-node-api-key-never-report"
    provider = ProviderConfig.from_api_inputs(
        api_key=secret,
        base_url="https://example.test/v1",
        model="gpt-image",
        api_protocol="auto",
    )
    assert resolve_api_credential(provider) == secret
    assert secret not in repr(provider)
    assert secret not in str(provider.public_dict())
    assert provider.public_dict()["api_key_configured"] is True
    rotated = ProviderConfig.from_api_inputs(
        api_key="different-test-key",
        base_url="https://example.test/v1",
        model="gpt-image",
        api_protocol="auto",
    )
    assert provider != rotated


@pytest.mark.parametrize(
    "secret",
    [
        "opaqueCredentialValue987654321\r\nInjected: header",
        "opaqueCredentialValue987654321\ttrailer",
        "opaqueCredentialValue987654321\u2603",
    ],
)
def test_api_key_with_invalid_header_characters_is_rejected_without_echo(secret):
    with pytest.raises(ValueError) as captured:
        ProviderConfig.from_api_inputs(
            api_key=secret,
            base_url="https://example.test/v1",
            model="gpt-image",
            api_protocol="auto",
        )
    assert secret not in str(captured.value)
    assert "visible ASCII" in str(captured.value)


def test_api_provider_switch_defaults_and_legacy_endpoint_inference():
    default_provider = ProviderConfig.from_api_inputs(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="gpt-image",
        api_protocol="auto",
    )
    assert default_provider.use_custom_endpoints is False
    assert default_provider.use_async is True
    assert default_provider.generate_endpoint == ""
    assert default_provider.edit_endpoint == ""

    legacy_provider = ProviderConfig.from_api_inputs(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="gpt-image",
        api_protocol="auto",
        edit_endpoint="chat/completions",
    )
    assert legacy_provider.use_custom_endpoints is True
    assert legacy_provider.edit_endpoint == "chat/completions"


def test_disabled_custom_endpoints_are_ignored_without_being_executed():
    provider = ProviderConfig.from_api_inputs(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="gpt-image",
        api_protocol="auto",
        use_custom_endpoints=False,
        generate_endpoint="../not-an-executable-route",
        edit_endpoint="chat/completions?token=must-not-be-used",
    )
    assert provider.generate_endpoint == ""
    assert provider.edit_endpoint == ""
    public = provider.public_dict()
    assert public["use_custom_endpoints"] is False
    assert public["use_async"] is True
    assert "must-not-be-used" not in str(public)


def test_enabled_custom_endpoints_are_normalized_and_validated():
    provider = ProviderConfig.from_api_inputs(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="gpt-image",
        api_protocol="images",
        use_custom_endpoints=True,
        use_async=False,
        generate_endpoint="/v1/images/generations",
    )
    assert provider.generate_endpoint == "v1/images/generations"
    assert provider.public_dict()["use_custom_endpoints"] is True
    assert provider.public_dict()["use_async"] is False
    with pytest.raises(ValueError, match="credentials"):
        ProviderConfig.from_api_inputs(
            api_key="test-key",
            base_url="https://example.test/v1",
            model="gpt-image",
            api_protocol="images",
            use_custom_endpoints=True,
            edit_endpoint="images/edits?token=unsafe",
        )


def test_custom_async_configuration_is_normalized_and_not_exposed_raw():
    provider = ProviderConfig.from_api_inputs(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="gpt-image",
        api_protocol="images",
        use_custom_endpoints=True,
        use_async=True,
        generate_endpoint="vendor/generate",
        async_generate_endpoint="/v1/vendor/jobs",
        async_poll_endpoint_template="vendor/jobs/status?id={task_id}",
        async_mapping_json=(
            '{"task_id_paths":["/job/id"],"status_paths":["/job/state"],'
            '"success_statuses":["DONE"],"failure_statuses":["FAILED"]}'
        ),
    )
    assert provider.async_generate_endpoint == "v1/vendor/jobs"
    assert (
        provider.async_poll_endpoint_template
        == "vendor/jobs/status?id={task_id}"
    )
    assert provider.async_mapping is not None
    assert provider.async_mapping.success_statuses == frozenset({"done"})
    public = provider.public_dict()
    assert public["async_mapping"] == {
        "configured": True,
        "keys": [
            "failure_statuses",
            "status_paths",
            "success_statuses",
            "task_id_paths",
        ],
    }
    assert "/job/id" not in str(public)
    assert "/job/id" not in repr(provider)


def test_empty_async_mapping_object_is_treated_as_not_configured():
    provider = ProviderConfig.from_api_inputs(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="gpt-image",
        api_protocol="images",
        use_custom_endpoints=True,
        use_async=True,
        async_mapping_json="{}",
    )
    assert provider.async_mapping is None
    assert provider.public_dict()["async_mapping"]["configured"] is False


@pytest.mark.parametrize(
    ("mapping", "message"),
    [
        ('{"unknown":["/id"]}', "unsupported keys"),
        ('{"task_id_paths":["job.id"]}', "RFC 6901"),
        ('{"task_id_paths":[]}', "non-empty array"),
        (
            '{"success_statuses":["done"],"failure_statuses":["DONE"]}',
            "must not overlap",
        ),
    ],
)
def test_invalid_custom_async_mapping_is_rejected_before_execution(mapping, message):
    with pytest.raises(ValueError, match=message):
        ProviderConfig.from_api_inputs(
            api_key="test-key",
            base_url="https://example.test/v1",
            model="gpt-image",
            api_protocol="images",
            use_custom_endpoints=True,
            use_async=True,
            async_generate_endpoint="vendor/jobs",
            async_poll_endpoint_template="vendor/jobs/{task_id}",
            async_mapping_json=mapping,
        )


def test_unknown_async_mapping_key_is_not_echoed_in_the_error():
    private_name = "opaqueUnknownMappingFieldValueMustNotEcho"
    with pytest.raises(ValueError) as captured:
        ProviderConfig.from_api_inputs(
            api_key="test-key",
            base_url="https://example.test/v1",
            model="gpt-image",
            api_protocol="images",
            use_custom_endpoints=True,
            use_async=True,
            async_generate_endpoint="vendor/jobs",
            async_poll_endpoint_template="vendor/jobs/{task_id}",
            async_mapping_json=f'{{"{private_name}":["/id"]}}',
        )
    assert private_name not in str(captured.value)
    assert "unsupported keys" in str(captured.value)


@pytest.mark.parametrize(
    "template",
    [
        "vendor/jobs",
        "vendor/jobs/{task_id}/{task_id}",
        "vendor/jobs?{task_id}=value",
        "vendor/jobs?task={task_id}#fragment",
    ],
)
def test_invalid_async_poll_template_is_rejected(template):
    with pytest.raises(ValueError):
        ProviderConfig.from_api_inputs(
            api_key="test-key",
            base_url="https://example.test/v1",
            model="gpt-image",
            api_protocol="images",
            use_custom_endpoints=True,
            use_async=True,
            async_generate_endpoint="vendor/jobs",
            async_poll_endpoint_template=template,
        )


def test_disabled_custom_or_async_settings_ignore_stale_async_text():
    disabled_custom = ProviderConfig.from_api_inputs(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="gpt-image",
        api_protocol="auto",
        use_custom_endpoints=False,
        use_async=True,
        async_poll_endpoint_template="not-a-valid-template",
        async_mapping_json="{not-json",
    )
    assert disabled_custom.async_poll_endpoint_template == ""
    assert disabled_custom.async_mapping is None

    disabled_async = ProviderConfig.from_api_inputs(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="gpt-image",
        api_protocol="auto",
        use_custom_endpoints=True,
        use_async=False,
        async_poll_endpoint_template="not-a-valid-template",
        async_mapping_json="{not-json",
    )
    assert disabled_async.async_poll_endpoint_template == ""
    assert disabled_async.async_mapping is None


def test_api_provider_resolves_a_non_secret_session_handle():
    clear_session_credentials()


def test_comfy_node_path_refuses_plaintext_when_frontend_handle_is_missing():
    with pytest.raises(BridgeError) as captured:
        ProviderConfig.from_api_inputs(
            api_key="raw-key-that-must-not-enter-a-comfy-prompt",
            base_url="https://example.test/v1",
            model="gpt-image",
            api_protocol="auto",
            require_session_handle=True,
        )
    assert captured.value.error_code == "session_credential_required"
    assert "raw-key-that-must-not-enter-a-comfy-prompt" not in str(captured.value)
    secret = "test-session-secret-not-for-network"
    handle = issue_session_credential(secret)
    provider = ProviderConfig.from_api_inputs(
        api_key=handle,
        base_url="https://example.test/v1",
        model="gpt-image",
        api_protocol="auto",
    )
    assert secret not in repr(provider)
    assert resolve_api_credential(provider) == secret
    with pytest.raises(BridgeError, match="session_credential_missing"):
        ProviderConfig.from_api_inputs(
            api_key=handle,
            base_url="https://example.test/v1",
            model="gpt-image",
            api_protocol="auto",
        )
    clear_session_credentials()


def test_oauth_provider_has_no_api_key_configuration():
    provider = ProviderConfig.from_oauth_inputs(
        model="gpt-image-2",
        api_protocol="auto",
    )
    assert provider.api_key == ""
    assert provider.use_custom_endpoints is False
    assert provider.use_async is False
    assert provider.public_dict() == {
        "auth_mode": "codex_oauth",
        "base_url": DEFAULT_API_BASE_URL,
        "model": "gpt-image-2",
        "api_protocol": "auto",
    }


def test_oauth_provider_rejects_api_only_chat_protocol():
    with pytest.raises(ValueError, match="Unsupported api_protocol"):
        ProviderConfig.from_oauth_inputs(
            model="gpt-image-2",
            api_protocol="chat_completions",
        )
