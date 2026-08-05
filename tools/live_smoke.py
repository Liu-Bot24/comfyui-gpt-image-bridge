from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from gpt_image_bridge.config import ProviderConfig  # noqa: E402
from gpt_image_bridge.errors import BridgeError, report_json  # noqa: E402
from gpt_image_bridge.images import EncodedImage  # noqa: E402
from gpt_image_bridge.protocols import execute_operation  # noqa: E402


def fixture_image(
    *,
    width: int,
    height: int,
    background: tuple[int, int, int],
    label: str,
    role: str,
    source_slot: int,
) -> EncodedImage:
    from io import BytesIO

    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    step = max(12, min(width, height) // 6)
    for value in range(-height, width, step):
        draw.line((value, 0, value + height, height), fill=(255, 255, 255), width=2)
    draw.rectangle((4, 4, min(width - 4, 90), 28), fill=(0, 0, 0))
    draw.text((9, 9), label, fill=(255, 255, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    return EncodedImage(
        output.getvalue(),
        width,
        height,
        source_slot=source_slot,
        role=role,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["oauth-edit", "api-generate", "api-edit"], required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="gpt-image-2")
    parser.add_argument("--protocol", choices=["auto", "responses", "images"], default="auto")
    parser.add_argument("--api-key-env", default="GPT_IMAGE_BRIDGE_TEST_API_KEY")
    parser.add_argument("--with-references", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=PROJECT / "artifacts" / "live")
    args = parser.parse_args()

    base = fixture_image(
        width=256,
        height=256,
        background=(30, 90, 170),
        label="BASE",
        role="base",
        source_slot=1,
    )
    references = (
        fixture_image(
            width=128,
            height=192,
            background=(175, 45, 45),
            label="REF-1",
            role="reference",
            source_slot=1,
        ),
        fixture_image(
            width=192,
            height=128,
            background=(40, 155, 80),
            label="REF-2",
            role="reference",
            source_slot=2,
        ),
    )
    if args.mode == "oauth-edit":
        provider = ProviderConfig.from_oauth_inputs(
            model=args.model,
            api_protocol=args.protocol,
        )
        operation = "edit"
    else:
        api_key = os.environ.get(args.api_key_env, "").strip()
        if not api_key or not args.base_url.strip() or not args.model.strip():
            print(
                "LIVE_SMOKE_FAILED reason=missing_api_inputs "
                "required=api_key_environment,base_url,model"
            )
            return 2
        provider = ProviderConfig.from_api_inputs(
            api_key=api_key,
            base_url=args.base_url,
            model=args.model,
            api_protocol=args.protocol,
        )
        operation = "generate" if args.mode == "api-generate" else "edit"

    prompt = (
        "Keep the blue BASE image as the composition and primary subject. Add one small "
        "red accent and one small green accent inspired by the two ordered references. "
        "Do not copy or return either reference image and keep the BASE label visible."
    )
    selected_references = references if operation == "edit" or args.with_references else ()
    kwargs = {
        "provider": provider,
        "operation": operation,
        "prompt": prompt,
        "references": selected_references,
        "size": "1024x1024",
        "quality": "low",
        "background": "opaque",
        "output_format": "png",
        "moderation": "auto",
        "n": 1,
        "timeout": 300,
    }
    if operation == "edit":
        kwargs["base_image"] = base
    try:
        result = execute_operation(**kwargs)
    except BridgeError as error:
        known = (provider.api_key,) if provider.api_key else ()
        print("LIVE_SMOKE_FAILED " + report_json(error.report(known), known))
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / f"{args.mode}-{args.protocol}-output.png"
    report_path = args.output_dir / f"{args.mode}-{args.protocol}-report.json"
    output_path.write_bytes(result.images[0])
    input_hashes = {
        "base": hashlib.sha256(base.png_bytes).hexdigest(),
        "reference_1": hashlib.sha256(references[0].png_bytes).hexdigest(),
        "reference_2": hashlib.sha256(references[1].png_bytes).hexdigest(),
    }
    output_hash = hashlib.sha256(result.images[0]).hexdigest()
    evidence = dict(result.report)
    evidence["smoke_assertions"] = {
        "input_sha256": input_hashes,
        "output_sha256": output_hash,
        "output_differs_from_every_input": output_hash not in set(input_hashes.values()),
    }
    report_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "LIVE_SMOKE_OK "
        f"mode={args.mode} protocol={result.report['selected_protocol']} "
        f"output={output_path.name} report={report_path.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
