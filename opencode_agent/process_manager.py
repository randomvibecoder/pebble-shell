from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROCESS_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass(slots=True)
class ManagedProcess:
    name: str
    command: str
    started_at: float
    log_path: Path
    process: subprocess.Popen[str]


class BackgroundProcessManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.processes: dict[str, ManagedProcess] = {}
        self.state_dir = self.root / ".opencode_agent" / "processes"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def start(self, name: str, command: str) -> dict[str, Any]:
        _validate_name(name)
        command = command.strip()
        if not command:
            raise ValueError("process command cannot be empty")
        existing = self.processes.get(name)
        if existing and existing.process.poll() is None:
            raise ValueError(f"Process already running: {name}")

        log_path = self.state_dir / f"{name}.log"
        log_file = log_path.open("ab")
        process = subprocess.Popen(
            command,
            cwd=self.root,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=False,
            executable="/bin/bash",
            start_new_session=True,
        )
        log_file.close()
        managed = ManagedProcess(name=name, command=command, started_at=time.time(), log_path=log_path, process=process)
        self.processes[name] = managed
        self._write_metadata(managed)
        return self.status(name)

    def list(self) -> list[dict[str, Any]]:
        return [self.status(name) for name in sorted(self.processes)]

    def status(self, name: str) -> dict[str, Any]:
        _validate_name(name)
        managed = self.processes.get(name)
        if not managed:
            raise ValueError(f"Unknown process: {name}")
        return _status_dict(managed)

    def logs(self, name: str, max_chars: int = 4000) -> str:
        _validate_name(name)
        managed = self.processes.get(name)
        if not managed:
            raise ValueError(f"Unknown process: {name}")
        if not managed.log_path.is_file():
            return ""
        data = managed.log_path.read_bytes()
        max_chars = max(1, min(max_chars, 20000))
        return data[-max_chars:].decode("utf-8", errors="replace")

    def stop(self, name: str, timeout_seconds: float = 5.0) -> dict[str, Any]:
        _validate_name(name)
        managed = self.processes.get(name)
        if not managed:
            raise ValueError(f"Unknown process: {name}")
        if managed.process.poll() is None:
            os.killpg(managed.process.pid, signal.SIGTERM)
            try:
                managed.process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                os.killpg(managed.process.pid, signal.SIGKILL)
                managed.process.wait(timeout=timeout_seconds)
        self._write_metadata(managed)
        return _status_dict(managed)

    def _write_metadata(self, managed: ManagedProcess) -> None:
        metadata = _status_dict(managed)
        metadata["log_path"] = str(managed.log_path)
        (self.state_dir / f"{managed.name}.json").write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")


def _status_dict(managed: ManagedProcess) -> dict[str, Any]:
    returncode = managed.process.poll()
    return {
        "name": managed.name,
        "command": managed.command,
        "pid": managed.process.pid,
        "running": returncode is None,
        "returncode": returncode,
        "started_at": managed.started_at,
        "log_file": f".opencode_agent/processes/{managed.name}.log",
    }


def _validate_name(name: str) -> None:
    if not PROCESS_NAME_RE.fullmatch(name):
        raise ValueError("process name must be 1-64 chars and contain only letters, numbers, underscores, or hyphens")
