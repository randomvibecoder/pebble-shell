from __future__ import annotations

from pathlib import Path


CONTEXT_FILES = ("SOUL.md", "AGENTS.md", "USER.md", "TOOLS.md")


class ContextFileLoader:
    def __init__(self, workspace: Path, bundled_root: Path, max_chars_per_file: int = 6000) -> None:
        self.workspace = workspace
        self.bundled_root = bundled_root
        self.max_chars_per_file = max_chars_per_file
        self._snapshot = self._load_current()

    def load(self) -> list[dict[str, str]]:
        return [dict(message) for message in self._snapshot]

    def refresh(self) -> None:
        self._snapshot = self._load_current()

    def _load_current(self) -> list[dict[str, str]]:
        blocks = []
        for name in CONTEXT_FILES:
            content = self._read_first(name)
            if content:
                blocks.append({"role": "system", "content": f"{name}:\n{content}"})
        return blocks

    def _read_first(self, name: str) -> str:
        for root in (self.workspace, self.bundled_root):
            path = root / name
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="replace").strip()
                if len(content) > self.max_chars_per_file:
                    content = content[: self.max_chars_per_file] + "\n[truncated]"
                return content
        return ""
