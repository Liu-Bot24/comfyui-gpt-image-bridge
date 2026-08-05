from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from conftest import image_tensor


PROJECT = Path(__file__).resolve().parents[1]


def import_comfy_package():
    package_name = "comfyui_gpt_image_bridge_test_package"
    spec = importlib.util.spec_from_file_location(
        package_name,
        PROJECT / "__init__.py",
        submodule_search_locations=[str(PROJECT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_comfyui_registration_and_provider_responsibilities_are_separate():
    module = import_comfy_package()
    expected = {
        "GPTImageBridgeAPIProvider",
        "GPTImageBridgeOAuthProvider",
        "GPTImageBridgeReferenceList",
        "GPTImageBridgeGenerate",
        "GPTImageBridgeEdit",
    }
    assert set(module.NODE_CLASS_MAPPINGS) == expected
    for name, node in module.NODE_CLASS_MAPPINGS.items():
        input_types = node.INPUT_TYPES()
        assert isinstance(input_types, dict)
        assert node.RETURN_TYPES
        assert isinstance(node.FUNCTION, str)
        assert name in module.NODE_DISPLAY_NAME_MAPPINGS

    api_inputs = module.NODE_CLASS_MAPPINGS[
        "GPTImageBridgeAPIProvider"
    ].INPUT_TYPES()["required"]
    assert list(api_inputs) == ["api_key", "base_url", "model", "api_protocol"]
    assert api_inputs["api_key"][1]["default"] == ""
    assert api_inputs["api_key"][1]["socketless"] is True
    assert api_inputs["base_url"][1]["default"] == ""
    assert api_inputs["model"][1]["default"] == ""
    assert api_inputs["api_protocol"][0] == (
        "auto",
        "responses",
        "images",
        "chat_completions",
    )
    api_optional = module.NODE_CLASS_MAPPINGS[
        "GPTImageBridgeAPIProvider"
    ].INPUT_TYPES()["optional"]
    assert list(api_optional) == [
        "use_custom_endpoints",
        "generate_endpoint",
        "edit_endpoint",
        "use_async",
        "async_generate_endpoint",
        "async_edit_endpoint",
        "async_poll_endpoint_template",
        "async_mapping_json",
    ]
    assert api_optional["use_custom_endpoints"][0] == "BOOLEAN"
    assert api_optional["use_custom_endpoints"][1]["default"] is False
    assert api_optional["use_custom_endpoints"][1]["socketless"] is True
    assert (
        api_optional["use_custom_endpoints"][1]["label"]
        == "使用自定义端点"
    )
    assert api_optional["use_async"][0] == "BOOLEAN"
    assert api_optional["use_async"][1]["default"] is True
    assert api_optional["use_async"][1]["socketless"] is True
    assert api_optional["use_async"][1]["label"] == "使用异步"
    assert api_optional["generate_endpoint"][1]["default"] == ""
    assert (
        api_optional["generate_endpoint"][1]["label"]
        == "自定义生成端点"
    )
    assert api_optional["edit_endpoint"][1]["default"] == ""
    assert api_optional["edit_endpoint"][1]["label"] == "自定义编辑端点"
    for name in (
        "use_custom_endpoints",
        "generate_endpoint",
        "edit_endpoint",
        "use_async",
        "async_generate_endpoint",
        "async_edit_endpoint",
        "async_poll_endpoint_template",
        "async_mapping_json",
    ):
        assert api_optional[name][1]["tooltip"].strip()
    assert "credential_service" not in api_inputs
    assert "credential_env" not in api_inputs
    assert module.WEB_DIRECTORY == "./gpt_image_bridge/web"
    widget_path = PROJECT / "gpt_image_bridge" / "web" / "api_key_widget.js"
    assert widget_path.is_file()
    widget_source = widget_path.read_text(encoding="utf-8")
    assert 'input.type = "password"' in widget_source
    assert "widget.type = PASSWORD_WIDGET_TYPE" in widget_source
    assert 'const PASSWORD_MASK = "••••••••••••"' in widget_source
    assert "widget.inputEl" not in widget_source
    for name, label in {
        "use_custom_endpoints": "使用自定义端点",
        "generate_endpoint": "自定义生成端点",
        "edit_endpoint": "自定义编辑端点",
        "use_async": "使用异步",
        "async_generate_endpoint": "自定义异步生成提交端点",
        "async_edit_endpoint": "自定义异步编辑提交端点",
        "async_poll_endpoint_template": "自定义异步查询端点模板",
        "async_mapping_json": "自定义异步 JSON 映射",
    }.items():
        assert name in widget_source
        assert label in widget_source
    assert "loadedGraphNode(node)" in widget_source
    assert "afterConfigureGraph()" in widget_source
    assert "applyProviderPresentation(node);" in widget_source

    oauth_inputs = module.NODE_CLASS_MAPPINGS[
        "GPTImageBridgeOAuthProvider"
    ].INPUT_TYPES()["required"]
    assert list(oauth_inputs) == [
        "model",
        "api_protocol",
    ]
    assert "api_key" not in oauth_inputs
    assert "base_url" not in oauth_inputs
    assert "auth_file" not in oauth_inputs
    assert oauth_inputs["api_protocol"][0] == ("auto", "responses", "images")

    reference_inputs = module.NODE_CLASS_MAPPINGS[
        "GPTImageBridgeReferenceList"
    ].INPUT_TYPES()["optional"]
    assert list(reference_inputs) == [
        "image_2",
        "image_3",
        "image_4",
        "image_5",
        "image_6",
        "image_7",
        "image_8",
        "image_9",
    ]
    assert module.NODE_DISPLAY_NAME_MAPPINGS["GPTImageBridgeReferenceList"] == (
        "GPT Image Bridge · Edit Reference List (Image 2–9)"
    )

    edit_types = module.NODE_CLASS_MAPPINGS[
        "GPTImageBridgeEdit"
    ].INPUT_TYPES()
    assert "base_image（Image 1）" in edit_types["required"]
    assert "base_image" not in edit_types["required"]
    assert "references（Image 2 开始）" in edit_types["optional"]
    assert "references" not in edit_types["optional"]
    generate_types = module.NODE_CLASS_MAPPINGS[
        "GPTImageBridgeGenerate"
    ].INPUT_TYPES()
    assert list(generate_types["optional"]) == [
        *(f"reference_{index}" for index in range(1, 10)),
    ]
    assert "references" not in generate_types["optional"]


def test_api_provider_frontend_migrates_legacy_widgets_before_configuration():
    widget_source = (
        PROJECT / "gpt_image_bridge" / "web" / "api_key_widget.js"
    ).read_text(encoding="utf-8")
    migration_source = (
        PROJECT / "gpt_image_bridge" / "web" / "provider_widget_migration.js"
    ).read_text(encoding="utf-8")

    # Migration must happen before LiteGraph assigns positional widget values.
    # Both the six-item legacy layout and the eight-item switch layout are
    # upgraded to the current grouped twelve-item layout.
    assert "migrateLegacyWorkflowData" in widget_source
    assert "values.length === 6" in migration_source
    assert "values.length === 8" in migration_source
    assert "PROVIDER_WIDGET_COUNT = 12" in migration_source
    assert "resolvePasswordWidgetWidth" in widget_source
    assert "drawingOnMainGraphCanvas" in widget_source

    configure_wrapper = widget_source.index(
        "const originalConfigure = nodeType.prototype.configure;"
    )
    on_configure_wrapper = widget_source.index(
        "const originalOnConfigure = nodeType.prototype.onConfigure;"
    )
    assert configure_wrapper < on_configure_wrapper
    assert (
        "const migrated = migrateLegacyWorkflowData(data);"
        in widget_source[configure_wrapper:on_configure_wrapper]
    )
    assert (
        "originalConfigure?.call(this, migrated)"
        in widget_source[configure_wrapper:on_configure_wrapper]
    )
    configure_body = widget_source[configure_wrapper:on_configure_wrapper]
    configure_call = configure_body.index(
        "const result = originalConfigure?.call(this, migrated);"
    )
    assert configure_body.index(
        "applyProviderWidgetLabels(this);",
        configure_call,
    ) > configure_call


def test_numbered_input_names_execute_in_registered_order(monkeypatch):
    module = import_comfy_package()
    node_module = sys.modules[f"{module.__name__}.nodes"]

    references = module.NODE_CLASS_MAPPINGS["GPTImageBridgeReferenceList"]().collect(
        image_2=image_tensor(11, 7, 0.1),
        image_3=image_tensor(5, 13, 0.2),
    )[0]
    assert [
        (item.source_slot, item.width, item.height)
        for item in references
    ] == [(2, 11, 7), (3, 5, 13)]

    captured = {}

    def fake_run_and_convert(**kwargs):
        captured.update(kwargs)
        return ("image", "revised", "report")

    monkeypatch.setattr(node_module, "_run_and_convert", fake_run_and_convert)
    provider = node_module.ProviderConfig(
        auth_mode="api_key",
        base_url="https://example.test/v1",
        model="gpt-image-test",
        api_protocol="images",
        api_key="session-handle",
    )
    result = module.NODE_CLASS_MAPPINGS["GPTImageBridgeEdit"]().edit(
        provider=provider,
        prompt="keep Image 1 as the base",
        size="auto",
        quality="low",
        background="auto",
        output_format="png",
        moderation="auto",
        n=1,
        timeout_sec=300,
        **{
            "base_image（Image 1）": image_tensor(9, 6, 0.3),
            "references（Image 2 开始）": references,
        },
    )

    assert result == ("image", "revised", "report")
    assert (captured["base_image"].width, captured["base_image"].height) == (9, 6)
    assert captured["references"] == references


def test_edit_reference_list_rejects_gaps_and_batches():
    module = import_comfy_package()
    edit_list = module.NODE_CLASS_MAPPINGS["GPTImageBridgeReferenceList"]()

    with pytest.raises(ValueError, match=r"connect image_3 before image_4"):
        edit_list.collect(
            image_2=image_tensor(11, 7, 0.1),
            image_4=image_tensor(5, 13, 0.2),
        )

    batch = image_tensor(4, 6, 0.1).repeat(2, 1, 1, 1)
    with pytest.raises(ValueError, match=r"image_2 accepts exactly one IMAGE"):
        edit_list.collect(image_2=batch)


def test_generate_direct_references_use_reference_local_numbers(monkeypatch):
    module = import_comfy_package()
    node_module = sys.modules[f"{module.__name__}.nodes"]
    captured = {}

    def fake_run_and_convert(**kwargs):
        captured.update(kwargs)
        return ("image", "revised", "report")

    monkeypatch.setattr(node_module, "_run_and_convert", fake_run_and_convert)
    provider = node_module.ProviderConfig(
        auth_mode="api_key",
        base_url="https://example.test/v1",
        model="gpt-image-test",
        api_protocol="responses",
        api_key="session-handle",
    )
    result = module.NODE_CLASS_MAPPINGS["GPTImageBridgeGenerate"]().generate(
        provider=provider,
        prompt="create a new image",
        size="auto",
        quality="low",
        background="auto",
        output_format="png",
        moderation="auto",
        n=1,
        timeout_sec=300,
        reference_1=image_tensor(11, 7, 0.1),
        reference_2=image_tensor(5, 13, 0.2),
    )

    assert result == ("image", "revised", "report")
    assert [
        (item.source_slot, item.width, item.height)
        for item in captured["references"]
    ] == [(1, 11, 7), (2, 5, 13)]


def test_generate_direct_references_reject_gaps_and_batches(monkeypatch):
    module = import_comfy_package()
    node_module = sys.modules[f"{module.__name__}.nodes"]

    def fake_run_and_convert(**kwargs):
        return ("image", "revised", "report")

    monkeypatch.setattr(node_module, "_run_and_convert", fake_run_and_convert)
    provider = node_module.ProviderConfig(
        auth_mode="api_key",
        base_url="https://example.test/v1",
        model="gpt-image-test",
        api_protocol="responses",
        api_key="session-handle",
    )
    common = {
        "provider": provider,
        "prompt": "create a new image",
        "size": "auto",
        "quality": "low",
        "background": "auto",
        "output_format": "png",
        "moderation": "auto",
        "n": 1,
        "timeout_sec": 300,
    }

    generate = module.NODE_CLASS_MAPPINGS["GPTImageBridgeGenerate"]().generate
    with pytest.raises(
        ValueError,
        match=r"connect reference_2 before reference_3",
    ):
        generate(
            **common,
            reference_1=image_tensor(11, 7, 0.1),
            reference_3=image_tensor(5, 13, 0.2),
        )
    batch = image_tensor(4, 6, 0.1).repeat(2, 1, 1, 1)
    with pytest.raises(ValueError, match=r"reference_1 accepts exactly one IMAGE"):
        generate(**common, reference_1=batch)


def test_generate_accepts_nine_direct_references_in_order(monkeypatch):
    module = import_comfy_package()
    node_module = sys.modules[f"{module.__name__}.nodes"]
    captured = {}

    def fake_run_and_convert(**kwargs):
        captured.update(kwargs)
        return ("image", "revised", "report")

    monkeypatch.setattr(node_module, "_run_and_convert", fake_run_and_convert)
    provider = node_module.ProviderConfig(
        auth_mode="api_key",
        base_url="https://example.test/v1",
        model="gpt-image-test",
        api_protocol="responses",
        api_key="session-handle",
    )
    direct_references = {
        f"reference_{index}": image_tensor(index + 3, index + 4, index / 10)
        for index in range(1, 10)
    }
    result = module.NODE_CLASS_MAPPINGS["GPTImageBridgeGenerate"]().generate(
        provider=provider,
        prompt="create a new image",
        size="auto",
        quality="low",
        background="auto",
        output_format="png",
        moderation="auto",
        n=1,
        timeout_sec=300,
        **direct_references,
    )

    assert result == ("image", "revised", "report")
    assert [
        (item.source_slot, item.width, item.height)
        for item in captured["references"]
    ] == [
        (index, index + 3, index + 4)
        for index in range(1, 10)
    ]
