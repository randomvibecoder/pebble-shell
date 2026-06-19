from pebble_shell.agent import _is_heartbeat_ack


def test_heartbeat_ack_is_suppressed_at_edges() -> None:
    assert _is_heartbeat_ack("HEARTBEAT_OK", 300)
    assert _is_heartbeat_ack("HEARTBEAT_OK Nothing to report.", 300)
    assert _is_heartbeat_ack("Nothing to report. HEARTBEAT_OK", 300)


def test_heartbeat_ack_in_middle_is_not_suppressed() -> None:
    assert not _is_heartbeat_ack("Investigated HEARTBEAT_OK but found a blocker.", 300)

