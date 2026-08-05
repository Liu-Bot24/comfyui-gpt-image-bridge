from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass

import numpy as np
import pytest
import torch
from PIL import Image

from gpt_image_bridge.config import ProviderConfig


def png_bytes(width=8, height=8, color=(20, 40, 60)):
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="PNG")
    return output.getvalue()


def png_b64(width=8, height=8, color=(20, 40, 60)):
    return base64.b64encode(png_bytes(width, height, color)).decode("ascii")


def image_tensor(width, height, value=0.5):
    return torch.full((1, height, width, 3), float(value), dtype=torch.float32)


@dataclass
class FakeResponse:
    body: bytes
    status: int = 200
    headers: dict | None = None

    def __post_init__(self):
        self.headers = self.headers or {"content-type": "application/json"}
        self._stream = io.BytesIO(self.body)

    def read(self, size=-1):
        return self._stream.read(size)

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class QueueOpener:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def api_provider():
    return ProviderConfig(
        auth_mode="api_key",
        base_url="https://example.test/v1",
        model="gpt-image-test",
        api_protocol="auto",
        api_key="test-secret-not-for-network",
        use_custom_endpoints=True,
        use_async=False,
    )


@pytest.fixture
def image_json():
    return json.dumps(
        {"data": [{"b64_json": png_b64(), "revised_prompt": "revised"}]}
    ).encode()
