from __future__ import annotations

import re
import time
import urllib.request
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from . import __version__
from .agent import ImageInput
from .images import image_input_from_bytes, is_supported_image


class AttachmentLike(Protocol):
    filename: str | None
    content_type: str | None
    url: str
    size: int | None


@dataclass(frozen=True, slots=True)
class SavedAttachments:
    lines: list[str]
    images: list[ImageInput]


def save_discord_attachments(
    attachments: list[AttachmentLike],
    workspace: Path,
    attachments_dir: str,
    channel_id: str,
    message_id: str | None,
    max_bytes: int,
) -> SavedAttachments:
    lines: list[str] = []
    images: list[ImageInput] = []
    if not attachments:
        return SavedAttachments(lines, images)

    folder = _attachment_folder(workspace, attachments_dir, channel_id, message_id)
    folder.mkdir(parents=True, exist_ok=True)

    for index, attachment in enumerate(attachments, start=1):
        filename = _safe_filename(attachment.filename or f"attachment-{index}")
        if attachment.size is not None and attachment.size > max_bytes:
            lines.append(f"[attached file skipped: {filename} exceeded {max_bytes} bytes]")
            continue
        try:
            data, content_type = _download_attachment(attachment.url, attachment.content_type or "", max_bytes)
        except Exception as exc:  # noqa: BLE001 - message should include concise download failure.
            lines.append(f"[attached file skipped: {filename} download failed: {str(exc)[:200]}]")
            continue

        path = _unique_path(folder / filename)
        path.write_bytes(data)
        if is_supported_image(filename, content_type):
            relative = path.relative_to(workspace).as_posix()
            lines.append(
                f"[attached image file: {relative}; already included as an image in this message, "
                "so do not call read_image or read for this image unless the user asks about the saved file later]"
            )
            try:
                images.append(image_input_from_bytes(data, relative, content_type, filename, max_bytes))
            except Exception:
                images.append(ImageInput(url=relative, content_type=content_type, filename=filename, source_url=relative))
        else:
            relative = path.relative_to(workspace).as_posix()
            lines.append(f"[attached file: {relative}]")
    return SavedAttachments(lines, images)


def append_attachment_lines(content: str, lines: list[str]) -> str:
    if not lines:
        return content
    base = content.strip()
    attachment_text = "\n".join(lines)
    if not base:
        return attachment_text
    return f"{base}\n\n{attachment_text}"


def _attachment_folder(workspace: Path, attachments_dir: str, channel_id: str, message_id: str | None) -> Path:
    now = datetime.now(timezone.utc)
    safe_dir = _safe_path_part(attachments_dir.strip("/") or "sent_attachments")
    if message_id:
        digest = hashlib.sha256(f"{channel_id}:{message_id}".encode("utf-8")).hexdigest()[:16]
        upload_id = f"upload-{digest}"
    else:
        upload_id = _safe_path_part(f"upload-{int(time.time() * 1000)}")
    return workspace / safe_dir / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}" / upload_id


def _safe_filename(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    name = name.strip(" .")
    if not name:
        return "attachment"
    if name in {".", ".."}:
        return "attachment"
    return name[:180]


def _safe_path_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    cleaned = cleaned.strip("._")
    return cleaned[:120] or "unknown"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10_000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find available filename for {path.name}")


def _download_attachment(url: str, content_type: str, max_bytes: int) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"user-agent": f"PebbleShell/{__version__}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(max_bytes + 1)
        response_type = response.headers.get("content-type", "").split(";", 1)[0]
    if len(data) > max_bytes:
        raise ValueError(f"exceeded {max_bytes} bytes")
    return data, content_type or response_type
