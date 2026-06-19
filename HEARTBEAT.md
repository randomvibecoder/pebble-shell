# HEARTBEAT

On heartbeat:

1. Inspect current workspace state only if useful.
2. Check whether there are open tasks, failures, or blockers that need user attention.
3. Take at most one small, safe, verifiable action.
4. If nothing needs attention, respond exactly `HEARTBEAT_OK`.
5. If attention is needed, report the before/after state and the next concrete action.

