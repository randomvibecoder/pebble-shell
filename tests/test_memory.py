from pathlib import Path

from pebble_shell.memory import MemoryStore


def test_memory_keeps_recent_messages_in_order(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.add_message("user", "first")
    store.add_message("assistant", "second")

    context = store.get_context("anything", recent_limit=10)

    assert context.recent_messages == [("user", "first"), ("assistant", "second")]


def test_memory_limits_recent_messages_by_token_budget(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.add_message("user", "first " + ("x" * 80))
    store.add_message("assistant", "second")
    store.add_message("user", "third")

    context = store.get_context("anything", recent_limit=10, recent_token_budget=8)

    assert context.recent_messages == [("assistant", "second"), ("user", "third")]


def test_memory_truncates_single_recent_message_over_budget(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.add_message("user", "prefix " + ("x" * 200))

    context = store.get_context("anything", recent_limit=10, recent_token_budget=12)

    assert len(context.recent_messages) == 1
    assert context.recent_messages[0][1].startswith("[older text truncated]")


def test_raw_tool_messages_are_preserved(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    raw = {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {"name": "list_files"}}], "content": None}

    store.add_message("assistant", "role: assistant\ntool_calls: ...", raw)

    context = store.get_context("", recent_limit=10)

    assert context.recent_raw_messages == [raw]


def test_summary_tracks_checkpoint(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.add_message("user", "remember project alpha")
    store.add_message("assistant", "ok")

    store.upsert_summary("Project alpha matters.", 1)

    context = store.get_context("", recent_limit=10)

    assert context.summary == "Project alpha matters."
    assert context.recent_messages == [("assistant", "ok")]


def test_last_message_id_tracks_latest_message(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")

    assert store.last_message_id() == 0
    store.add_message("user", "hello")

    assert store.last_message_id() == 1


def test_contacts_are_persisted(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")

    store.set_contact("initial_dm_sent:test", "sent")

    assert store.get_contact("initial_dm_sent:test") == "sent"
