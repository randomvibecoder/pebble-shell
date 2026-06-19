from __future__ import annotations

from pathlib import Path
from typing import Any


def list_public_sites(workspace: Path) -> list[dict[str, Any]]:
    public_root = workspace / "public"
    if not public_root.is_dir():
        return []

    sites = []
    for child in sorted(public_root.iterdir(), key=lambda item: item.name):
        if child.name.startswith(".") or not child.is_dir():
            continue
        files = [
            path
            for path in child.rglob("*")
            if path.is_file() and not any(part.startswith(".") for part in path.relative_to(child).parts)
        ]
        entry = "index.html" if (child / "index.html").is_file() else (files[0].relative_to(child).as_posix() if files else "")
        sites.append(
            {
                "name": child.name,
                "url": f"/public/{child.name}/" + entry,
                "file_count": len(files),
                "has_index": (child / "index.html").is_file(),
            }
        )
    return sites
