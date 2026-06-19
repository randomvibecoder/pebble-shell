from __future__ import annotations

import re
from pathlib import Path

from .context_files import CONTEXT_DIR

SKILL_EXTENSIONS = {".md", ".txt"}
MAX_SKILL_BYTES = 512_000
MAX_LOADED_SKILL_CHARS = 12_000
SKILL_NAME_RE = re.compile(r"\bname:\s*([a-zA-Z0-9_-]{1,64})")
WORD_RE = re.compile(r"[a-zA-Z0-9_]{3,}")


class SkillLoader:
    def __init__(self, workspace: Path, bundled_root: Path) -> None:
        self.workspace = workspace
        self.bundled_root = bundled_root

    def load(self, query: str | None = None, max_relevant: int = 3) -> str:
        paths = self._skill_paths()
        if not paths:
            return "No skills available."

        names = [path.stem for path in paths]
        chunks = ["Available skills:\n" + "\n".join(f"- {name}" for name in names)]
        loaded: list[tuple[Path, str]] = []

        for path in paths:
            if path.name.upper() == "SKILLS.MD":
                content = _read_skill(path)
                if content:
                    loaded.append((path, content))

        for path in self._relevant_skill_paths(query or "", paths, max_relevant):
            if path.name.upper() == "SKILLS.MD" or any(existing == path for existing, _ in loaded):
                continue
            content = _read_skill(path)
            if content:
                loaded.append((path, content))

        if loaded:
            rendered = []
            budget = MAX_LOADED_SKILL_CHARS
            for path, content in loaded:
                header = f"## {path.stem}\n"
                remaining = budget - len(header)
                if remaining <= 0:
                    break
                body = content[:remaining]
                if len(content) > remaining:
                    body += "\n[skill truncated]"
                rendered.append(header + body)
                budget -= len(header) + len(body)
            chunks.append("Loaded skill instructions:\n\n" + "\n\n".join(rendered))

        return "\n\n".join(chunks)

    def list(self) -> list[str]:
        return [path.stem for path in self._skill_paths()]

    def list_details(self) -> list[dict[str, str | bool]]:
        details = []
        for path in self._all_skill_paths():
            disabled = self._is_disabled(path.stem)
            details.append(
                {
                    "name": path.stem,
                    "enabled": not disabled,
                    "source": "workspace" if self._is_workspace_skill(path) else "bundled",
                    "path": path.relative_to(self.workspace).as_posix()
                    if self._is_workspace_skill(path)
                    else path.relative_to(self.bundled_root).as_posix(),
                }
            )
        return details

    def view(self, name: str) -> str:
        normalized = Path(name).stem
        for path in self._skill_paths():
            if path.stem == normalized:
                return path.read_text(encoding="utf-8", errors="replace").strip()
        raise ValueError(f"Unknown skill: {name}")

    def save(self, name: str, content: str) -> Path:
        normalized = _normalize_skill_name(name)
        skills_dir = self.workspace / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        path = skills_dir / f"{normalized}.md"
        path.write_text(content.strip() + "\n", encoding="utf-8")
        self.enable(normalized)
        return path

    def install_from_path(self, source: Path, name: str | None = None) -> Path:
        if not source.is_file():
            raise ValueError(f"Not a file: {source}")
        if source.suffix.lower() not in SKILL_EXTENSIONS and source.name.upper() != "SKILL.MD":
            raise ValueError("skill path must point to a .md, .txt, or SKILL.md file")
        data = source.read_bytes()
        if len(data) > MAX_SKILL_BYTES:
            raise ValueError(f"skill document exceeds {MAX_SKILL_BYTES} bytes")
        content = data.decode("utf-8", errors="replace").strip()
        if not content:
            raise ValueError("skill document is empty")
        skill_name = name or _skill_name_from_content(content) or _skill_name_from_path(source)
        return self.save(skill_name, content)

    def disable(self, name: str) -> str:
        normalized = _normalize_skill_name(name)
        if not self._find_skill_path(normalized, include_disabled=True):
            raise ValueError(f"Unknown skill: {normalized}")
        marker = self._disabled_marker(normalized)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("disabled\n", encoding="utf-8")
        return normalized

    def enable(self, name: str) -> str:
        normalized = _normalize_skill_name(name)
        marker = self._disabled_marker(normalized)
        if marker.exists():
            marker.unlink()
        return normalized

    def delete(self, name: str) -> str:
        normalized = _normalize_skill_name(name)
        path = self._find_skill_path(normalized, include_disabled=True)
        if not path:
            raise ValueError(f"Unknown skill: {normalized}")
        if not self._is_workspace_skill(path):
            raise ValueError(f"Cannot delete bundled skill {normalized}; disable it instead")
        if path.name.upper() == "SKILLS.MD":
            raise ValueError("Cannot delete default SKILLS.md through skill_delete")
        path.unlink()
        marker = self._disabled_marker(normalized)
        if marker.exists():
            marker.unlink()
        return normalized

    def load_all_for_tests(self) -> str:
        chunks: list[str] = []
        for path in self._skill_paths():
            try:
                content = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if content:
                chunks.append(f"## {path.name}\n{content}")
        if not chunks:
            return "No skills loaded."
        return "Loaded skills:\n\n" + "\n\n".join(chunks)

    def _skill_paths(self) -> list[Path]:
        return [path for path in self._all_skill_paths() if not self._is_disabled(path.stem)]

    def _all_skill_paths(self) -> list[Path]:
        paths: list[Path] = []
        seen_names: set[str] = set()

        def add(path: Path) -> None:
            if path.is_file() and path.stem not in seen_names:
                paths.append(path)
                seen_names.add(path.stem)

        for root in (self.workspace, self.bundled_root):
            add(root / CONTEXT_DIR / "SKILLS.md")
            add(root / "SKILLS.md")
            skills_dir = root / "skills"
            if skills_dir.is_dir():
                for path in sorted(path for path in skills_dir.rglob("*") if path.suffix in SKILL_EXTENSIONS):
                    add(path)
        return paths

    def _disabled_marker(self, name: str) -> Path:
        return self.workspace / ".pebble_shell" / "disabled_skills" / f"{name}.disabled"

    def _is_disabled(self, name: str) -> bool:
        return self._disabled_marker(name).is_file()

    def _is_workspace_skill(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.workspace.resolve())
            return True
        except ValueError:
            return False

    def _find_skill_path(self, name: str, include_disabled: bool = False) -> Path | None:
        paths = self._all_skill_paths() if include_disabled else self._skill_paths()
        for path in paths:
            if path.stem == name:
                return path
        return None

    def _relevant_skill_paths(self, query: str, paths: list[Path], max_relevant: int) -> list[Path]:
        query_terms = set(WORD_RE.findall(query.lower()))
        scored: list[tuple[int, str, Path]] = []
        for path in paths:
            if path.name.upper() == "SKILLS.MD":
                continue
            content = _read_skill(path)
            haystack = f"{path.stem} {content}".lower()
            if not query_terms:
                score = 0
            else:
                score = sum(1 for term in query_terms if term in haystack)
            if score > 0:
                scored.append((score, path.as_posix(), path))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [path for _, _, path in scored[: max(0, max_relevant)]]


def _normalize_skill_name(name: str) -> str:
    normalized = Path(name).stem.strip().replace(" ", "-")
    if not normalized or any(char in normalized for char in "/\\"):
        raise ValueError("skill name must be a simple file name")
    return normalized


def _skill_name_from_content(content: str) -> str | None:
    match = SKILL_NAME_RE.search(content[:2000])
    return match.group(1) if match else None


def _skill_name_from_path(path: Path) -> str:
    if path.name.upper() == "SKILL.MD" and path.parent.name:
        return _normalize_skill_name(path.parent.name)
    return _normalize_skill_name(path.stem or "local-skill")


def _read_skill(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
