from __future__ import annotations

import sqlite3
from pathlib import Path


class ShellAuditStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def record(self, command: str, allowed: bool, risk: str, reason: str, exit_code: int | None, output: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into shell_audit(command, allowed, risk, reason, exit_code, output)
                values (?, ?, ?, ?, ?, ?)
                """,
                (command, int(allowed), risk, reason, exit_code, output[:4000]),
            )

    def recent(self, limit: int = 50) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select command, allowed, risk, reason, exit_code, output, created_at
                from shell_audit
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "command": row[0],
                "allowed": bool(row[1]),
                "risk": row[2],
                "reason": row[3],
                "exit_code": row[4],
                "output": row[5],
                "created_at": row[6],
            }
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists shell_audit (
                    id integer primary key autoincrement,
                    command text not null,
                    allowed integer not null,
                    risk text not null,
                    reason text not null,
                    exit_code integer,
                    output text not null,
                    created_at text not null default current_timestamp
                )
                """
            )
