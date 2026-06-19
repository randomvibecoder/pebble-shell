from __future__ import annotations

import base64
import urllib.request

from . import __version__
from .agent import ImageInput

SUPPORTED_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def is_supported_image(filename: str = "", content_type: str = "") -> bool:
    normalized_type = content_type.lower()
    normalized_name = filename.lower()
    return normalized_type.startswith("image/") or normalized_name.endswith(SUPPORTED_IMAGE_EXTENSIONS)


def image_input_from_bytes(
    data: bytes,
    source_url: str,
    content_type: str = "",
    filename: str = "",
    max_bytes: int = 4_000_000,
) -> ImageInput:
    if len(data) > max_bytes:
        raise ValueError(f"image exceeds max size of {max_bytes} bytes")
    media_type = content_type or _content_type_from_filename(filename) or "image/jpeg"
    encoded = base64.b64encode(data).decode("ascii")
    return ImageInput(
        url=f"data:{media_type};base64,{encoded}",
        content_type=media_type,
        filename=filename,
        source_url=source_url,
    )


def fetch_image_input(url: str, content_type: str = "", filename: str = "", max_bytes: int = 4_000_000) -> ImageInput:
    request = urllib.request.Request(url, headers={"user-agent": f"PebbleShell/{__version__}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(max_bytes + 1)
        response_type = response.headers.get("content-type", "").split(";", 1)[0]
    return image_input_from_bytes(data, url, content_type or response_type, filename, max_bytes)


def image_url_input(url: str, content_type: str = "", filename: str = "") -> ImageInput:
    return ImageInput(url=url, content_type=content_type, filename=filename, source_url=url)


def _content_type_from_filename(filename: str) -> str:
    name = filename.lower()
    if name.endswith(".png"):
        return "image/png"
    if name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if name.endswith(".webp"):
        return "image/webp"
    if name.endswith(".gif"):
        return "image/gif"
    return ""
