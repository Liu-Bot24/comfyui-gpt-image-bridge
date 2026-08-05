from __future__ import annotations

import json
import re
from pathlib import Path

from PIL import Image

from gpt_image_bridge.protocols import CLIENT_HEADER

from tools.create_example_workflows import (
    ASSET_FILENAMES,
    EXAMPLE_FILENAME,
    LEGACY_EXAMPLE_FILENAMES,
    PACKAGE_VERSION,
    REGISTRY_NODE_ID,
    build_workflow,
)


PROJECT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = PROJECT / "example_workflows"


def test_combined_example_structure_and_branch_modes():
    workflow = build_workflow()
    nodes = {node["id"]: node for node in workflow["nodes"]}
    types = [node["type"] for node in workflow["nodes"]]

    assert len(nodes) == 11
    assert len(workflow["links"]) == 10
    assert types.count("LoadImage") == 3
    assert types.count("GPTImageBridgeOAuthProvider") == 1
    assert types.count("GPTImageBridgeAPIProvider") == 1
    assert types.count("GPTImageBridgeReferenceList") == 1
    assert types.count("GPTImageBridgeEdit") == 2
    assert types.count("SaveImage") == 2
    assert types.count("MarkdownNote") == 1
    assert not any(
        token in type_name.lower()
        for type_name in types
        for token in ("checkpoint", "vae", "lora", "clip", "sampler")
    )

    assert [nodes[node_id]["mode"] for node_id in (5, 6, 7)] == [0, 0, 0]
    assert [nodes[node_id]["mode"] for node_id in (8, 9, 10)] == [2, 2, 2]
    assert nodes[11]["mode"] == 0


def test_custom_nodes_identify_their_registry_package():
    workflow = build_workflow()
    custom_nodes = [
        node for node in workflow["nodes"] if node["type"].startswith("GPTImageBridge")
    ]
    core_nodes = [
        node for node in workflow["nodes"] if node["type"] in {"LoadImage", "SaveImage"}
    ]
    note = next(node for node in workflow["nodes"] if node["type"] == "MarkdownNote")

    assert custom_nodes
    for node in custom_nodes:
        assert node["properties"]["Node name for S&R"] == node["type"]
        assert node["properties"]["cnr_id"] == REGISTRY_NODE_ID
        assert node["properties"]["ver"] == PACKAGE_VERSION
    assert core_nodes
    assert all(node["properties"]["cnr_id"] == "comfy-core" for node in core_nodes)
    assert "cnr_id" not in note["properties"]


def test_release_version_matches_client_header_without_rewriting_workflow():
    pyproject = (PROJECT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, flags=re.MULTILINE)

    assert match is not None
    assert CLIENT_HEADER == f"comfyui-gpt-image-bridge/{match.group(1)}"


def test_combined_example_links_and_roles_are_exact():
    workflow = build_workflow()
    nodes = {node["id"]: node for node in workflow["nodes"]}

    assert workflow["links"] == [
        [1, 1, 0, 6, 1, "IMAGE"],
        [2, 1, 0, 9, 1, "IMAGE"],
        [3, 2, 0, 4, 0, "IMAGE"],
        [4, 3, 0, 4, 1, "IMAGE"],
        [5, 4, 0, 6, 10, "GPT_IMAGE_REFERENCES"],
        [6, 4, 0, 9, 10, "GPT_IMAGE_REFERENCES"],
        [7, 5, 0, 6, 0, "GPT_IMAGE_PROVIDER"],
        [8, 6, 0, 7, 0, "IMAGE"],
        [9, 8, 0, 9, 0, "GPT_IMAGE_PROVIDER"],
        [10, 9, 0, 10, 0, "IMAGE"],
    ]

    assert nodes[1]["outputs"][0]["links"] == [1, 2]
    assert nodes[2]["outputs"][0]["links"] == [3]
    assert nodes[3]["outputs"][0]["links"] == [4]
    assert nodes[4]["outputs"][0]["links"] == [5, 6]
    assert nodes[5]["outputs"][0]["links"] == [7]
    assert nodes[6]["outputs"][0]["links"] == [8]
    assert nodes[8]["outputs"][0]["links"] == [9]
    assert nodes[9]["outputs"][0]["links"] == [10]

    assert nodes[4]["inputs"][0]["link"] == 3
    assert nodes[4]["inputs"][1]["link"] == 4
    assert nodes[4]["inputs"][0]["name"] == "image_2"
    assert nodes[4]["inputs"][0]["label"] == "image_2（Image 2）"
    assert nodes[4]["inputs"][1]["name"] == "image_3"
    assert nodes[4]["inputs"][1]["label"] == "image_3（Image 3）"
    assert nodes[6]["inputs"][0]["link"] == 7
    assert nodes[6]["inputs"][1]["link"] == 1
    assert nodes[6]["inputs"][1]["name"] == "base_image（Image 1）"
    assert nodes[6]["inputs"][1]["label"] == "base_image（Image 1）"
    assert nodes[6]["inputs"][10]["link"] == 5
    assert nodes[6]["inputs"][10]["name"] == "references（Image 2 开始）"
    assert nodes[6]["inputs"][10]["label"] == "references（Image 2 开始）"
    assert nodes[9]["inputs"][0]["link"] == 9
    assert nodes[9]["inputs"][1]["link"] == 2
    assert nodes[9]["inputs"][1]["name"] == "base_image（Image 1）"
    assert nodes[9]["inputs"][1]["label"] == "base_image（Image 1）"
    assert nodes[9]["inputs"][10]["link"] == 6
    assert nodes[9]["inputs"][10]["name"] == "references（Image 2 开始）"
    assert nodes[9]["inputs"][10]["label"] == "references（Image 2 开始）"
    assert nodes[7]["inputs"][0]["link"] == 8
    assert nodes[10]["inputs"][0]["link"] == 10


def test_combined_example_provider_values_are_safe():
    nodes = {node["id"]: node for node in build_workflow()["nodes"]}

    assert nodes[5]["widgets_values"] == ["gpt-image-2", "auto"]
    assert [item["name"] for item in nodes[5]["inputs"]] == [
        "model",
        "api_protocol",
    ]
    assert nodes[8]["widgets_values"] == [
        "",
        "",
        "",
        "auto",
        False,
        "",
        "",
        True,
        "",
        "",
        "",
        "",
    ]
    assert [item["name"] for item in nodes[8]["inputs"]] == [
        "api_key",
        "base_url",
        "model",
        "api_protocol",
        "use_custom_endpoints",
        "generate_endpoint",
        "edit_endpoint",
        "use_async",
        "async_generate_endpoint",
        "async_edit_endpoint",
        "async_poll_endpoint_template",
        "async_mapping_json",
    ]
    assert {
        item["name"]: item["label"]
        for item in nodes[8]["inputs"]
        if item["name"]
        in {
            "use_custom_endpoints",
            "generate_endpoint",
            "edit_endpoint",
            "use_async",
            "async_generate_endpoint",
            "async_edit_endpoint",
            "async_poll_endpoint_template",
            "async_mapping_json",
        }
    } == {
        "use_custom_endpoints": "使用自定义端点",
        "generate_endpoint": "自定义生成端点",
        "edit_endpoint": "自定义编辑端点",
        "use_async": "使用异步",
        "async_generate_endpoint": "自定义异步生成提交端点",
        "async_edit_endpoint": "自定义异步编辑提交端点",
        "async_poll_endpoint_template": "自定义异步查询端点模板",
        "async_mapping_json": "自定义异步 JSON 映射",
    }
    assert nodes[8]["inputs"][0]["name"] == "api_key"
    assert nodes[8]["widgets_values"][0] == ""


def test_combined_example_warns_before_sharing_without_resetting_configuration():
    nodes = {node["id"]: node for node in build_workflow()["nodes"]}
    note = nodes[11]

    assert note["type"] == "MarkdownNote"
    assert note["inputs"] == []
    assert note["outputs"] == []
    text = note["widgets_values"][0]
    assert "正常保存 API 配置" in text
    assert "手动清空" in text
    assert "API Key" in text
    assert "异步 JSON 映射" in text


def test_api_provider_and_sharing_note_do_not_overlap():
    nodes = {node["id"]: node for node in build_workflow()["nodes"]}
    provider = nodes[8]
    note = nodes[11]
    provider_bottom = provider["pos"][1] + provider["size"][1]
    note_top = note["pos"][1]
    assert provider_bottom < note_top


def test_generated_example_matches_the_only_checked_in_workflow():
    expected = build_workflow()
    actual = json.loads((EXAMPLE_DIR / EXAMPLE_FILENAME).read_text(encoding="utf-8"))
    assert actual == expected
    assert sorted(path.name for path in EXAMPLE_DIR.glob("*.json")) == [EXAMPLE_FILENAME]
    assert not any((EXAMPLE_DIR / name).exists() for name in LEGACY_EXAMPLE_FILENAMES)


def test_example_contains_no_secret_machine_path_or_unrelated_workflow_content():
    text = (EXAMPLE_DIR / EXAMPLE_FILENAME).read_text(encoding="utf-8")
    assert not re.search(r"(?i)[a-z]:\\", text)
    assert not re.search(r"\bsk-[A-Za-z0-9._~-]{16,}\b", text)
    assert "credential_service" not in text
    assert "credential_env" not in text


def test_example_assets_are_distinct_pngs_with_different_dimensions():
    expected_dimensions = {
        ASSET_FILENAMES["base"]: (256, 256),
        ASSET_FILENAMES["reference_1"]: (128, 192),
        ASSET_FILENAMES["reference_2"]: (192, 128),
    }
    asset_dir = PROJECT / "examples" / "assets"
    contents = set()
    for filename, dimensions in expected_dimensions.items():
        path = asset_dir / filename
        assert path.is_file()
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.size == dimensions
            assert image.info == {}
            assert len(image.getexif()) == 0
        contents.add(path.read_bytes())
    assert len(contents) == 3
