from __future__ import annotations

from pathlib import Path


CONTEXT_DIR = "context"
CONTEXT_FILES = ("SOUL.md", "AGENTS.md", "USER.md", "TOOLS.md")
WORKSPACE_CONTEXT_FILES = (*CONTEXT_FILES, "SKILLS.md", "HEARTBEAT.md", "MEMORY.md")


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
                blocks.append({"role": "system", "content": f"{CONTEXT_DIR}/{name}:\n{content}"})
        return blocks

    def _read_first(self, name: str) -> str:
        for path in context_file_candidates(self.workspace, self.bundled_root, name):
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="replace").strip()
                if len(content) > self.max_chars_per_file:
                    content = content[: self.max_chars_per_file] + "\n[truncated]"
                return content
        return ""


def context_file_candidates(workspace: Path, bundled_root: Path, name: str) -> list[Path]:
    return [
        workspace / CONTEXT_DIR / name,
        workspace / name,
        bundled_root / CONTEXT_DIR / name,
        bundled_root / name,
    ]


def ensure_workspace_context_files(workspace: Path, bundled_root: Path) -> None:
    target_dir = workspace / CONTEXT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in WORKSPACE_CONTEXT_FILES:
        target = target_dir / name
        if target.exists():
            continue
        for source in (workspace / name, bundled_root / CONTEXT_DIR / name, bundled_root / name):
            if source.is_file():
                target.write_text(source.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                break
