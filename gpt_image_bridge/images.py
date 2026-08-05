from __future__ import annotations

import base64
import io
from dataclasses import dataclass, replace
from typing import Any, Iterable

import numpy as np
from PIL import Image


MAX_INPUT_IMAGES = 9
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000


@dataclass(frozen=True)
class EncodedImage:
    png_bytes: bytes
    width: int
    height: int
    source_slot: int
    batch_index: int = 0
    role: str = "reference"

    @property
    def b64(self) -> str:
        return base64.b64encode(self.png_bytes).decode("ascii")

    @property
    def data_url(self) -> str:
        return f"data:image/png;base64,{self.b64}"

    def public_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "source_slot": self.source_slot,
            "batch_index": self.batch_index,
            "width": self.width,
            "height": self.height,
            "mime_type": "image/png",
            "encoded_bytes": len(self.png_bytes),
        }


def _tensor_frames(tensor: Any) -> Iterable[Any]:
    if tensor is None:
        return []
    ndim = int(tensor.ndim)
    if ndim == 3:
        return [tensor]
    if ndim == 4:
        return [tensor[index] for index in range(int(tensor.shape[0]))]
    raise ValueError(f"IMAGE tensor must have 3 or 4 dimensions, got {ndim}.")


def tensor_to_png(
    tensor: Any,
    *,
    source_slot: int = 0,
    batch_index: int = 0,
    role: str = "reference",
) -> EncodedImage:
    if int(tensor.ndim) == 4:
        if int(tensor.shape[0]) != 1:
            raise ValueError("Expected one IMAGE frame; use encode_reference_slots for batches.")
        tensor = tensor[0]
    array = tensor.detach().cpu().numpy()
    if array.ndim != 3 or array.shape[-1] not in (1, 3, 4):
        raise ValueError("IMAGE tensor must use ComfyUI [height,width,channels] layout.")
    array = np.clip(array * 255.0, 0, 255).round().astype(np.uint8)
    if array.shape[-1] == 1:
        array = array[:, :, 0]
    pil = Image.fromarray(array)
    if pil.mode not in {"RGB", "RGBA"}:
        pil = pil.convert("RGB")
    output = io.BytesIO()
    pil.save(output, format="PNG")
    return EncodedImage(
        png_bytes=output.getvalue(),
        width=pil.width,
        height=pil.height,
        source_slot=source_slot,
        batch_index=batch_index,
        role=role,
    )


def encode_reference_slots(
    *slots: Any,
    start_slot: int = 2,
) -> tuple[EncodedImage, ...]:
    if int(start_slot) < 1:
        raise ValueError("start_slot must be at least 1.")
    encoded: list[EncodedImage] = []
    for slot_index, tensor in enumerate(slots, start=int(start_slot)):
        if tensor is None:
            continue
        for batch_index, frame in enumerate(_tensor_frames(tensor)):
            encoded.append(
                tensor_to_png(
                    frame,
                    source_slot=slot_index,
                    batch_index=batch_index,
                    role="reference",
                )
            )
            if len(encoded) > MAX_INPUT_IMAGES:
                raise ValueError(f"At most {MAX_INPUT_IMAGES} reference images are supported.")
    return tuple(encoded)


def renumber_references(
    references: Iterable[EncodedImage],
    *,
    start_slot: int,
) -> tuple[EncodedImage, ...]:
    """Assign operation-local reference numbers without changing image bytes or order."""
    if int(start_slot) < 1:
        raise ValueError("start_slot must be at least 1.")
    return tuple(
        replace(
            reference,
            source_slot=int(start_slot) + index,
            batch_index=0,
            role="reference",
        )
        for index, reference in enumerate(references)
    )


def mask_to_png(mask: Any) -> bytes:
    if mask is None:
        raise ValueError("mask cannot be None.")
    if int(mask.ndim) == 3:
        mask = mask[0]
    elif int(mask.ndim) != 2:
        raise ValueError("MASK tensor must have [batch,height,width] or [height,width] layout.")
    array = np.clip(mask.detach().cpu().numpy() * 255.0, 0, 255).round().astype(np.uint8)
    pil = Image.fromarray(array, mode="L")
    output = io.BytesIO()
    pil.save(output, format="PNG")
    return output.getvalue()


def validate_image_bytes(
    payload: bytes,
    *,
    content_type: str = "",
    max_bytes: int = MAX_IMAGE_BYTES,
) -> tuple[bytes, dict[str, Any]]:
    if not payload:
        raise ValueError("Image response was empty.")
    if len(payload) > max_bytes:
        raise ValueError(f"Image response exceeds the {max_bytes}-byte safety limit.")
    if content_type and not content_type.lower().split(";", 1)[0].strip().startswith("image/"):
        raise ValueError(f"Image URL returned unexpected MIME type {content_type!r}.")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
        with Image.open(io.BytesIO(payload)) as image:
            width, height = image.size
            image_format = image.format or "unknown"
    except Exception as error:
        raise ValueError("Returned bytes are not a valid image.") from error
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("Returned image dimensions exceed safety limits.")
    return payload, {
        "width": width,
        "height": height,
        "format": image_format,
        "mime_type": content_type.split(";", 1)[0].strip() or None,
        "bytes": len(payload),
    }


def b64_image_to_bytes(value: str) -> tuple[bytes, dict[str, Any]]:
    raw_value = value.split(",", 1)[1] if "," in value else value
    try:
        payload = base64.b64decode(raw_value, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise ValueError("Image response contains invalid base64 data.") from error
    return validate_image_bytes(payload)


def image_bytes_to_tensor(payloads: list[bytes]):
    import torch

    frames = []
    dimensions = set()
    for payload in payloads:
        with Image.open(io.BytesIO(payload)) as image:
            rgb = image.convert("RGB")
            dimensions.add(rgb.size)
            array = np.asarray(rgb).astype(np.float32) / 255.0
        frames.append(torch.from_numpy(array)[None,])
    if not frames:
        raise ValueError("No output images were decoded.")
    if len(dimensions) != 1:
        raise ValueError(
            "Provider returned images with different dimensions; ComfyUI cannot form one IMAGE batch."
        )
    return torch.cat(frames, dim=0)
