from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT / "example_workflows"
EXAMPLE_FILENAME = "GPT-Image-Bridge-Edit-Example.json"
REGISTRY_NODE_ID = "comfyui-gpt-image-bridge"
PACKAGE_VERSION = "0.5.1"
LEGACY_EXAMPLE_FILENAMES = (
    "GPT-Image-Bridge-OAuth-Edit-Example.json",
    "GPT-Image-Bridge-API-Edit-Example.json",
)
ASSET_FILENAMES = {
    "base": "gpt-image-bridge-example-base.png",
    "reference_1": "gpt-image-bridge-example-reference-1.png",
    "reference_2": "gpt-image-bridge-example-reference-2.png",
}
PROMPT = (
    "Edit Image 1 as the base image and preserve its composition and primary subject. "
    "Use Image 2 and then Image 3 as ordered visual references. Incorporate one "
    "distinctive detail from each reference, but do not return either reference unchanged."
)


def widget_input(
    name: str,
    type_name: str,
    *,
    label: str | None = None,
) -> dict[str, Any]:
    return {
        "label": label or name,
        "name": name,
        "type": type_name,
        "link": None,
        "widget": {"name": name},
    }


def linked_input(
    name: str,
    type_name: str,
    link: int | None,
    *,
    label: str | None = None,
) -> dict[str, Any]:
    return {
        "label": label or name,
        "name": name,
        "type": type_name,
        "link": link,
    }


def output(name: str, type_name: str, links: list[int]) -> dict[str, Any]:
    return {"label": name, "name": name, "type": type_name, "links": links}


def node_properties(type_name: str, *, core: bool = False) -> dict[str, str]:
    result = {"Node name for S&R": type_name}
    if core:
        result.update({"cnr_id": "comfy-core", "ver": "0.28.0"})
    elif type_name.startswith("GPTImageBridge"):
        result.update({"cnr_id": REGISTRY_NODE_ID, "ver": PACKAGE_VERSION})
    return result


def load_image_node(
    node_id: int,
    *,
    y: int,
    filename: str,
    title: str,
    output_links: list[int],
    order: int,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "LoadImage",
        "pos": [0, y],
        "size": [260, 230],
        "flags": {},
        "order": order,
        "mode": 0,
        "inputs": [
            widget_input("image", "COMBO"),
            widget_input("upload", "IMAGEUPLOAD"),
        ],
        "outputs": [
            output("IMAGE", "IMAGE", output_links),
            output("MASK", "MASK", []),
        ],
        "title": title,
        "properties": node_properties("LoadImage", core=True),
        "widgets_values": [filename, "image"],
    }


def provider_node(
    node_id: int,
    *,
    auth_mode: str,
    y: int,
    mode: int,
    output_link: int,
    order: int,
) -> dict[str, Any]:
    if auth_mode == "oauth":
        type_name = "GPTImageBridgeOAuthProvider"
        inputs = [
            widget_input("model", "STRING"),
            widget_input("api_protocol", "COMBO"),
        ]
        widgets = ["gpt-image-2", "auto"]
        title = "OAuth Provider（默认启用；自动读取登录）"
    elif auth_mode == "api":
        type_name = "GPTImageBridgeAPIProvider"
        inputs = [
            widget_input("api_key", "STRING"),
            widget_input("base_url", "STRING"),
            widget_input("model", "STRING"),
            widget_input("api_protocol", "COMBO"),
            widget_input(
                "use_custom_endpoints",
                "BOOLEAN",
                label="使用自定义端点",
            ),
            widget_input(
                "generate_endpoint",
                "STRING",
                label="自定义生成端点",
            ),
            widget_input(
                "edit_endpoint",
                "STRING",
                label="自定义编辑端点",
            ),
            widget_input("use_async", "BOOLEAN", label="使用异步"),
            widget_input(
                "async_generate_endpoint",
                "STRING",
                label="自定义异步生成提交端点",
            ),
            widget_input(
                "async_edit_endpoint",
                "STRING",
                label="自定义异步编辑提交端点",
            ),
            widget_input(
                "async_poll_endpoint_template",
                "STRING",
                label="自定义异步查询端点模板",
            ),
            widget_input(
                "async_mapping_json",
                "STRING",
                label="自定义异步 JSON 映射",
            ),
        ]
        widgets = [
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
        title = "API Provider（默认禁用；异步默认开启）"
    else:
        raise ValueError(f"Unsupported auth mode: {auth_mode}")
    return {
        "id": node_id,
        "type": type_name,
        "pos": [660, y],
        "size": [370, 430 if auth_mode == "api" else 190],
        "flags": {},
        "order": order,
        "mode": mode,
        "inputs": inputs,
        "outputs": [output("provider", "GPT_IMAGE_PROVIDER", [output_link])],
        "title": title,
        "properties": node_properties(type_name),
        "widgets_values": widgets,
    }


def reference_list_node() -> dict[str, Any]:
    inputs = [
        linked_input(
            f"image_{index}",
            "IMAGE",
            {2: 3, 3: 4}.get(index),
            label=f"image_{index}（Image {index}）",
        )
        for index in range(2, 10)
    ]
    return {
        "id": 4,
        "type": "GPTImageBridgeReferenceList",
        "pos": [300, 240],
        "size": [320, 300],
        "flags": {},
        "order": 3,
        "mode": 0,
        "inputs": inputs,
        "outputs": [output("references", "GPT_IMAGE_REFERENCES", [5, 6])],
        "title": "参考图队列（从 Image 2 开始）",
        "properties": node_properties("GPTImageBridgeReferenceList"),
        "widgets_values": [],
    }


def edit_node(
    node_id: int,
    *,
    auth_mode: str,
    y: int,
    mode: int,
    provider_link: int,
    base_link: int,
    reference_link: int,
    output_link: int,
    order: int,
) -> dict[str, Any]:
    branch_name = "OAuth" if auth_mode == "oauth" else "API"
    state = "默认启用" if mode == 0 else "默认禁用"
    return {
        "id": node_id,
        "type": "GPTImageBridgeEdit",
        "pos": [1060, y],
        "size": [470, 650],
        "flags": {},
        "order": order,
        "mode": mode,
        "inputs": [
            linked_input("provider", "GPT_IMAGE_PROVIDER", provider_link),
            linked_input(
                "base_image（Image 1）",
                "IMAGE",
                base_link,
                label="base_image（Image 1）",
            ),
            widget_input("prompt", "STRING"),
            widget_input("size", "COMBO"),
            widget_input("quality", "COMBO"),
            widget_input("background", "COMBO"),
            widget_input("output_format", "COMBO"),
            widget_input("moderation", "COMBO"),
            widget_input("n", "INT"),
            widget_input("timeout_sec", "INT"),
            linked_input(
                "references（Image 2 开始）",
                "GPT_IMAGE_REFERENCES",
                reference_link,
                label="references（Image 2 开始）",
            ),
            linked_input("mask", "MASK", None),
        ],
        "outputs": [
            output("image", "IMAGE", [output_link]),
            output("revised_prompt", "STRING", []),
            output("request_report", "STRING", []),
        ],
        "title": f"{branch_name} 图片编辑（{state}）",
        "properties": node_properties("GPTImageBridgeEdit"),
        "widgets_values": [
            PROMPT,
            "auto",
            "medium",
            "auto",
            "png",
            "auto",
            1,
            300,
        ],
    }


def save_image_node(
    node_id: int,
    *,
    auth_mode: str,
    y: int,
    mode: int,
    input_link: int,
    order: int,
) -> dict[str, Any]:
    branch_name = "OAuth" if auth_mode == "oauth" else "API"
    state = "默认启用" if mode == 0 else "默认禁用"
    return {
        "id": node_id,
        "type": "SaveImage",
        "pos": [1560, y],
        "size": [320, 180],
        "flags": {},
        "order": order,
        "mode": mode,
        "inputs": [
            linked_input("images", "IMAGE", input_link),
            widget_input("filename_prefix", "STRING"),
        ],
        "outputs": [output("images", "IMAGE", [])],
        "title": f"{branch_name} 保存结果（{state}）",
        "properties": node_properties("SaveImage", core=True),
        "widgets_values": [f"gpt-image-bridge-examples/{auth_mode}-edit"],
    }


def api_sharing_note_node() -> dict[str, Any]:
    return {
        "id": 11,
        "type": "MarkdownNote",
        "pos": [660, 1170],
        "size": [370, 150],
        "flags": {},
        "order": 10,
        "mode": 0,
        "inputs": [],
        "outputs": [],
        "title": "分享工作流前检查 API 配置",
        "properties": {},
        "widgets_values": [
            "**本地正式工作流会正常保存 API 配置。**\n\n"
            "分享工作流前，请手动清空 API Provider 中的 **API Key**；"
            "如果 Base URL、模型名称、自定义端点或异步 JSON 映射属于私有配置，"
            "也请一并清空。"
            "关闭“使用自定义端点”不会删除已经保存的端点文本。"
        ],
        "color": "#432",
        "bgcolor": "#653",
    }


def build_workflow() -> dict[str, Any]:
    workflow_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "comfyui-gpt-image-bridge/example/oauth-and-api-edit",
        )
    )
    return {
        "id": workflow_id,
        "revision": 0,
        "last_node_id": 11,
        "last_link_id": 10,
        "nodes": [
            load_image_node(
                1,
                y=0,
                filename=ASSET_FILENAMES["base"],
                title="Image 1｜基础图（待编辑）",
                output_links=[1, 2],
                order=0,
            ),
            load_image_node(
                2,
                y=250,
                filename=ASSET_FILENAMES["reference_1"],
                title="Image 2｜参考图 1（队列第 1 张）",
                output_links=[3],
                order=1,
            ),
            load_image_node(
                3,
                y=500,
                filename=ASSET_FILENAMES["reference_2"],
                title="Image 3｜参考图 2（队列第 2 张）",
                output_links=[4],
                order=2,
            ),
            reference_list_node(),
            provider_node(
                5,
                auth_mode="oauth",
                y=0,
                mode=0,
                output_link=7,
                order=4,
            ),
            edit_node(
                6,
                auth_mode="oauth",
                y=0,
                mode=0,
                provider_link=7,
                base_link=1,
                reference_link=5,
                output_link=8,
                order=5,
            ),
            save_image_node(
                7,
                auth_mode="oauth",
                y=0,
                mode=0,
                input_link=8,
                order=6,
            ),
            provider_node(
                8,
                auth_mode="api",
                y=720,
                mode=2,
                output_link=9,
                order=7,
            ),
            edit_node(
                9,
                auth_mode="api",
                y=720,
                mode=2,
                provider_link=9,
                base_link=2,
                reference_link=6,
                output_link=10,
                order=8,
            ),
            save_image_node(
                10,
                auth_mode="api",
                y=720,
                mode=2,
                input_link=10,
                order=9,
            ),
            api_sharing_note_node(),
        ],
        "links": [
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
        ],
        "groups": [
            {
                "id": 1,
                "title": "共享输入：Image 1 基础图 + Image 2/3 参考图",
                "bounding": [-30, -40, 670, 820],
                "color": "#315f78",
                "flags": {},
            },
            {
                "id": 2,
                "title": "OAuth 分支（默认启用）",
                "bounding": [630, -40, 1280, 710],
                "color": "#3f789e",
                "flags": {},
            },
            {
                "id": 3,
                "title": "API 分支（默认禁用；填写必要配置后启用）",
                "bounding": [630, 680, 1280, 710],
                "color": "#8a6d3b",
                "flags": {},
            },
        ],
        "config": {},
        "extra": {
            "ds": {"scale": 0.52, "offset": [50, 35]},
            "workflowRendererVersion": "LG",
            "frontendVersion": "1.45.21",
            "gpt_image_bridge_example": "oauth_and_api_edit_with_two_references",
            "gpt_image_bridge_default_branch": "oauth",
        },
        "version": 0.4,
    }


def write_example(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    for legacy_name in LEGACY_EXAMPLE_FILENAMES:
        legacy_path = output_dir / legacy_name
        if legacy_path.is_file():
            legacy_path.unlink()
    path = output_dir / EXAMPLE_FILENAME
    path.write_text(
        json.dumps(build_workflow(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create one combined GPT Image Bridge edit example."
    )
    parser.add_argument("output_dir", nargs="?", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    print(write_example(args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
