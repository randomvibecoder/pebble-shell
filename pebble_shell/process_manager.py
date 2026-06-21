from __future__ import annotations

import os
import pty
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_YIELD_MS = 10_000
MAX_YIELD_MS = 30_000
DEFAULT_OUTPUT_CHARS = 20_000
MAX_OUTPUT_CHARS = 100_000


@dataclass(slots=True)
class TerminalSession:
    session_id: int
    command: str
    started_at: float
    log_path: Path
    process: subprocess.Popen[bytes]
    tty: bool = False
    pty_master_fd: int | None = None
    output: bytearray = field(default_factory=bytearray)
    read_offset: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class BackgroundProcessManager:
    def __init__(self, root: Path, cwd: Path | None = None) -> None:
        self.root = root.resolve()
        self.cwd = (cwd or root).resolve()
        self.sessions: dict[int, TerminalSession] = {}
        self._next_session_id = 1
        self._lock = threading.Lock()
        self.state_dir = self.root / ".pebble_shell" / "terminal_sessions"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def exec_command(
        self,
        cmd: str,
        yield_time_ms: int = DEFAULT_YIELD_MS,
        max_output_tokens: int = DEFAULT_OUTPUT_CHARS,
        tty: bool = False,
        workdir: Path | None = None,
        shell: str | None = None,
        login: bool = True,
    ) -> dict[str, Any]:
        command = cmd.strip()
        if not command:
            raise ValueError("cmd cannot be empty")
        cwd = (workdir or self.cwd).resolve()
        if cwd != self.root and self.root not in cwd.parents:
            raise ValueError(f"workdir escapes workspace: {cwd}")
        cwd.mkdir(parents=True, exist_ok=True)
        executable = shell or "/bin/bash"
        session_id = self._allocate_session_id()
        log_path = self.state_dir / f"session_{session_id}.log"
        master_fd: int | None = None
        slave_fd: int | None = None
        if tty:
            master_fd, slave_fd = pty.openpty()
            process = subprocess.Popen(
                command,
                cwd=cwd,
                shell=True,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                executable=executable,
                start_new_session=True,
                close_fds=True,
            )
            os.close(slave_fd)
        else:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                executable=executable,
                start_new_session=True,
            )
        session = TerminalSession(
            session_id=session_id,
            command=command,
            started_at=time.time(),
            log_path=log_path,
            process=process,
            tty=tty,
            pty_master_fd=master_fd,
        )
        self.sessions[session_id] = session
        threading.Thread(target=self._reader, args=(session,), daemon=True).start()
        return self._wait_and_status(session, yield_time_ms, max_output_tokens, incremental=False, tty=tty)

    def write_stdin(
        self,
        session_id: int,
        chars: str = "",
        yield_time_ms: int = DEFAULT_YIELD_MS,
        max_output_tokens: int = DEFAULT_OUTPUT_CHARS,
    ) -> dict[str, Any]:
        session = self._session(session_id)
        if chars and session.process.poll() is None:
            if session.pty_master_fd is not None:
                os.write(session.pty_master_fd, chars.encode("utf-8", errors="replace"))
            elif session.process.stdin is None:
                raise ValueError(f"Session {session_id} does not accept stdin")
            else:
                session.process.stdin.write(chars.encode("utf-8", errors="replace"))
                session.process.stdin.flush()
        return self._wait_and_status(session, yield_time_ms, max_output_tokens, incremental=True)

    def list(self) -> list[dict[str, Any]]:
        return [self.status(session_id) for session_id in sorted(self.sessions)]

    def status(self, session_id: int) -> dict[str, Any]:
        session = self._session(session_id)
        return self._status_dict(session)

    def logs(self, session_id: int, max_chars: int = DEFAULT_OUTPUT_CHARS) -> str:
        session = self._session(session_id)
        with session.lock:
            output = bytes(session.output)
        return _decode_tail(output, _clamp_output_chars(max_chars))

    def stop(self, session_id: int, timeout_seconds: float = 5.0) -> dict[str, Any]:
        session = self._session(session_id)
        if session.process.poll() is None:
            os.killpg(session.process.pid, signal.SIGTERM)
            try:
                session.process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                os.killpg(session.process.pid, signal.SIGKILL)
                session.process.wait(timeout=timeout_seconds)
        return self._status_dict(session)

    def _reader(self, session: TerminalSession) -> None:
        if session.pty_master_fd is not None:
            fd = session.pty_master_fd
        else:
            assert session.process.stdout is not None
            fd = session.process.stdout.fileno()
        with session.log_path.open("ab") as log_file:
            while True:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if not ready:
                    if session.process.poll() is not None:
                        try:
                            chunk = os.read(fd, 4096)
                        except OSError:
                            break
                        if not chunk:
                            break
                        self._record_output(session, log_file, chunk)
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                self._record_output(session, log_file, chunk)

    def _record_output(self, session: TerminalSession, log_file: Any, chunk: bytes) -> None:
        log_file.write(chunk)
        log_file.flush()
        with session.lock:
            session.output.extend(chunk)

    def _wait_and_status(
        self,
        session: TerminalSession,
        yield_time_ms: int,
        max_output_tokens: int,
        incremental: bool,
        tty: bool = False,
    ) -> dict[str, Any]:
        deadline = time.time() + (_clamp_yield_ms(yield_time_ms) / 1000)
        while time.time() < deadline:
            if session.process.poll() is not None:
                time.sleep(0.02)
                break
            time.sleep(0.05)
        status = self._status_dict(session, max_output_tokens=max_output_tokens, incremental=incremental)
        return status

    def _status_dict(
        self,
        session: TerminalSession,
        max_output_tokens: int = DEFAULT_OUTPUT_CHARS,
        incremental: bool = False,
    ) -> dict[str, Any]:
        with session.lock:
            if incremental:
                output = bytes(session.output[session.read_offset :])
                session.read_offset = len(session.output)
            else:
                output = bytes(session.output)
        returncode = session.process.poll()
        running = returncode is None
        return {
            "session_id": session.session_id,
            "command": session.command,
            "pid": session.process.pid,
            "running": running,
            "returncode": returncode,
            "started_at": session.started_at,
            "log_file": f".pebble_shell/terminal_sessions/session_{session.session_id}.log",
            "output": _decode_tail(output, _clamp_output_chars(max_output_tokens)),
            "tty": session.tty,
        }

    def _session(self, session_id: int) -> TerminalSession:
        try:
            normalized = int(session_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid session_id: {session_id}") from exc
        session = self.sessions.get(normalized)
        if not session:
            raise ValueError(f"Unknown session_id: {session_id}")
        return session

    def _allocate_session_id(self) -> int:
        with self._lock:
            session_id = self._next_session_id
            self._next_session_id += 1
        return session_id


def _clamp_yield_ms(value: int) -> int:
    return max(0, min(int(value), MAX_YIELD_MS))


def _clamp_output_chars(value: int) -> int:
    return max(1, min(int(value), MAX_OUTPUT_CHARS))


def _decode_tail(output: bytes, max_chars: int) -> str:
    text = output.decode("utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]
