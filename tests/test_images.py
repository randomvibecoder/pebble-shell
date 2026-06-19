from opencode_agent.images import image_input_from_bytes, is_supported_image


def test_image_input_from_bytes_builds_data_url_and_preserves_source() -> None:
    image = image_input_from_bytes(b"cat", "https://example.com/cat.png", "image/png", "cat.png")

    assert image.url == "data:image/png;base64,Y2F0"
    assert image.source_url == "https://example.com/cat.png"
    assert image.content_type == "image/png"
    assert image.filename == "cat.png"


def test_is_supported_image_uses_content_type_or_filename() -> None:
    assert is_supported_image("file.bin", "image/png")
    assert is_supported_image("photo.webp", "")
    assert not is_supported_image("notes.txt", "text/plain")
