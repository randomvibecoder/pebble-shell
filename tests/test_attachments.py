from __future__ import annotations

from types import SimpleNamespace

import pebble_shell.attachments as attachments_module
from pebble_shell.attachments import append_attachment_lines, save_discord_attachments


def test_save_discord_attachments_saves_pdf_and_image(monkeypatch, tmp_path) -> None:
    responses = {
        "https://cdn.discordapp.com/report.pdf": (b"%PDF-1.4", "application/pdf"),
        "https://cdn.discordapp.com/cat.png": (b"cat-bytes", "image/png"),
    }

    monkeypatch.setattr(attachments_module, "_download_attachment", lambda url, content_type, max_bytes: responses[url])

    saved = save_discord_attachments(
        [
            SimpleNamespace(filename="report.pdf", content_type="application/pdf", url="https://cdn.discordapp.com/report.pdf", size=8),
            SimpleNamespace(filename="cat.png", content_type="image/png", url="https://cdn.discordapp.com/cat.png", size=9),
        ],
        tmp_path,
        "sent_attachments",
        "channel-1",
        "message-1",
        100,
    )

    assert len(saved.lines) == 2
    assert saved.lines[0].endswith("/report.pdf]")
    assert "/channel-1/" not in saved.lines[0]
    assert "/message-1/" not in saved.lines[0]
    assert saved.lines[1].startswith("[attached image file: sent_attachments/")
    assert saved.lines[1].split("file: ", 1)[1].split(";", 1)[0].endswith("/cat.png")
    assert "/channel-1/" not in saved.lines[1]
    assert "/message-1/" not in saved.lines[1]
    assert "do not call inspect_image or read_file" in saved.lines[1]
    assert (tmp_path / saved.lines[0].removeprefix("[attached file: ").removesuffix("]")).read_bytes() == b"%PDF-1.4"
    assert len(saved.images) == 1
    assert saved.images[0].filename == "cat.png"
    assert saved.images[0].content_type == "image/png"
    assert saved.images[0].source_url.endswith("/cat.png")
    assert "/channel-1/" not in saved.images[0].source_url
    assert "/message-1/" not in saved.images[0].source_url


def test_save_discord_attachments_uses_numeric_suffix_for_collisions(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(attachments_module, "_download_attachment", lambda url, content_type, max_bytes: (b"data", "text/plain"))

    first = save_discord_attachments(
        [SimpleNamespace(filename="notes.txt", content_type="text/plain", url="https://cdn.discordapp.com/1", size=4)],
        tmp_path,
        "sent_attachments",
        "channel-1",
        "message-1",
        100,
    )
    second = save_discord_attachments(
        [SimpleNamespace(filename="notes.txt", content_type="text/plain", url="https://cdn.discordapp.com/2", size=4)],
        tmp_path,
        "sent_attachments",
        "channel-1",
        "message-1",
        100,
    )

    assert first.lines[0].endswith("/notes.txt]")
    assert second.lines[0].endswith("/notes-2.txt]")


def test_save_discord_attachments_skips_oversized_file(tmp_path) -> None:
    saved = save_discord_attachments(
        [SimpleNamespace(filename="big.pdf", content_type="application/pdf", url="https://cdn.discordapp.com/big.pdf", size=101)],
        tmp_path,
        "sent_attachments",
        "channel-1",
        "message-1",
        100,
    )

    assert saved.lines == ["[attached file skipped: big.pdf exceeded 100 bytes]"]
    assert saved.images == []


def test_append_attachment_lines_supports_multiple_files() -> None:
    assert append_attachment_lines("read these", ["[attached file: a.pdf]", "[attached file: b.png]"]) == (
        "read these\n\n[attached file: a.pdf]\n[attached file: b.png]"
    )
