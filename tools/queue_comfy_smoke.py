from __future__ import annotations

import argparse
import hashlib
import io
import json
import mimetypes
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import Image, ImageDraw


PROJECT = Path(__file__).resolve().parents[1]
SERVER = "http://127.0.0.1:8188"


def fixture(width, height, color, label):
    image = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(image)
    step = max(12, min(width, height) // 6)
    for value in range(-height, width, step):
        draw.line((value, 0, value + height, height), fill=(255, 255, 255), width=2)
    draw.rectangle((4, 4, min(width - 4, 90), 28), fill=(0, 0, 0))
    draw.text((9, 9), label, fill=(255, 255, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def multipart_upload(filename, payload):
    boundary = f"----GPTImageBridgeSmoke{uuid.uuid4().hex}"
    fields = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="type"\r\n\r\n'
        "input\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="subfolder"\r\n\r\n'
        "gpt-image-bridge-smoke\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="overwrite"\r\n\r\n'
        "true\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        f"{SERVER}/upload/image",
        data=fields,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        uploaded = json.loads(response.read().decode())
    return f"{uploaded.get('subfolder', '')}/{uploaded['name']}".strip("/")


def json_request(path, payload=None, timeout=30):
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{SERVER}{path}",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def main():
    global SERVER
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default=SERVER)
    parser.add_argument("--auth-mode", choices=["oauth", "api"], default="oauth")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="gpt-image-2")
    parser.add_argument("--api-key-env", default="GPT_IMAGE_BRIDGE_TEST_API_KEY")
    args = parser.parse_args()
    SERVER = args.server.rstrip("/")
    fixtures = {
        "base.png": fixture(256, 256, (30, 90, 170), "BASE"),
        "reference-1.png": fixture(128, 192, (175, 45, 45), "REF-1"),
        "reference-2.png": fixture(192, 128, (40, 155, 80), "REF-2"),
    }
    uploaded = {
        name: multipart_upload(name, payload)
        for name, payload in fixtures.items()
    }
    if args.auth_mode == "oauth":
        provider_class = "GPTImageBridgeOAuthProvider"
        provider_inputs = {
            "model": args.model,
            "api_protocol": "auto",
        }
    else:
        api_key = os.environ.get(args.api_key_env, "").strip()
        if not api_key or not args.base_url.strip() or not args.model.strip():
            print(
                "COMFY_QUEUE_FAILED reason=missing_api_inputs "
                "required=api_key_environment,base_url,model"
            )
            return 2
        credential = json_request(
            "/gpt-image-bridge/session-credential",
            {"api_key": api_key},
        )
        credential_handle = credential.get("credential_handle", "")
        if not credential_handle:
            print("COMFY_QUEUE_FAILED reason=session_credential_unavailable")
            return 2
        provider_class = "GPTImageBridgeAPIProvider"
        provider_inputs = {
            "api_key": credential_handle,
            "base_url": args.base_url,
            "model": args.model,
            "api_protocol": "auto",
        }
    prompt = {
        "1": {"class_type": "LoadImage", "inputs": {"image": uploaded["base.png"]}},
        "2": {
            "class_type": "LoadImage",
            "inputs": {"image": uploaded["reference-1.png"]},
        },
        "3": {
            "class_type": "LoadImage",
            "inputs": {"image": uploaded["reference-2.png"]},
        },
        "4": {
            "class_type": provider_class,
            "inputs": provider_inputs,
        },
        "5": {
            "class_type": "GPTImageBridgeReferenceList",
            "inputs": {"image_2": ["2", 0], "image_3": ["3", 0]},
        },
        "6": {
            "class_type": "GPTImageBridgeEdit",
            "inputs": {
                "provider": ["4", 0],
                "base_image（Image 1）": ["1", 0],
                "prompt": (
                    "Keep the blue BASE image as the composition and primary subject. "
                    "Add one small red accent and one small green accent inspired by the "
                    "two ordered references. Do not copy or return either reference image "
                    "and keep the BASE label visible."
                ),
                "size": "1024x1024",
                "quality": "low",
                "background": "opaque",
                "output_format": "png",
                "moderation": "auto",
                "n": 1,
                "timeout_sec": 300,
                "references（Image 2 开始）": ["5", 0],
            },
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": f"gpt-image-bridge-smoke/{args.auth_mode}-workflow",
                "images": ["6", 0],
            },
        },
    }
    queued = json_request(
        "/prompt",
        {
            "prompt": prompt,
            "client_id": f"gpt-image-bridge-smoke-{uuid.uuid4().hex}",
        },
    )
    prompt_id = queued["prompt_id"]
    deadline = time.monotonic() + 360
    history = None
    while time.monotonic() < deadline:
        result = json_request(f"/history/{prompt_id}", timeout=10)
        if prompt_id in result:
            history = result[prompt_id]
            break
        time.sleep(1)
    if history is None:
        print(f"COMFY_QUEUE_FAILED prompt_id={prompt_id} reason=timeout")
        return 1
    status = history.get("status") or {}
    if status.get("status_str") != "success":
        messages = status.get("messages") or []
        safe_types = [
            message[0] if isinstance(message, list) and message else "unknown"
            for message in messages
        ]
        print(
            f"COMFY_QUEUE_FAILED prompt_id={prompt_id} "
            f"status={status.get('status_str')} events={safe_types}"
        )
        return 1
    entries = (history.get("outputs", {}).get("7", {}).get("images") or [])
    if not entries:
        print(f"COMFY_QUEUE_FAILED prompt_id={prompt_id} reason=no_saved_image")
        return 1
    entry = entries[0]
    query = urllib.parse.urlencode(
        {
            "filename": entry["filename"],
            "subfolder": entry.get("subfolder", ""),
            "type": entry.get("type", "output"),
        }
    )
    with urllib.request.urlopen(f"{SERVER}/view?{query}", timeout=30) as response:
        output = response.read()
    with Image.open(io.BytesIO(output)) as image:
        sanitized = io.BytesIO()
        image.copy().save(sanitized, format="PNG")
    output = sanitized.getvalue()
    with Image.open(io.BytesIO(output)) as image:
        if "prompt" in image.info or "workflow" in image.info:
            print("COMFY_QUEUE_FAILED reason=artifact_metadata_not_removed")
            return 1
    output_hash = hashlib.sha256(output).hexdigest()
    input_hashes = {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in fixtures.items()
    }
    server_port = urllib.parse.urlsplit(SERVER).port or 80
    artifact_dir = PROJECT / "artifacts" / f"comfyui-{server_port}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_stem = f"{args.auth_mode}-workflow"
    (artifact_dir / f"{artifact_stem}-output.png").write_bytes(output)
    evidence = {
        "ok": True,
        "server": SERVER,
        "prompt_id": prompt_id,
        "status": status.get("status_str"),
        "auth_mode": args.auth_mode,
        "input_roles": [
            {"role": "base", "width": 256, "height": 256},
            {"role": "reference", "slot": 1, "width": 128, "height": 192},
            {"role": "reference", "slot": 2, "width": 192, "height": 128},
        ],
        "input_sha256": input_hashes,
        "output_sha256": output_hash,
        "output_differs_from_every_input": output_hash not in set(input_hashes.values()),
        "saved_image": {
            "filename": entry["filename"],
            "subfolder": entry.get("subfolder", ""),
            "type": entry.get("type", "output"),
            "bytes": len(output),
        },
        "example_workflow_sha256": hashlib.sha256(
            (
                PROJECT
                / "example_workflows"
                / "GPT-Image-Bridge-Edit-Example.json"
            ).read_bytes()
        ).hexdigest(),
    }
    (artifact_dir / f"{artifact_stem}-report.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "COMFY_QUEUE_OK "
        f"prompt_id={prompt_id} output={entry['filename']} "
        f"differs_from_inputs={evidence['output_differs_from_every_input']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
