# AGENTS

Pebble Shell is a Docker-isolated coding and operations agent. Pebble-facing context files live in `context/`.

- Keep file and shell work inside the configured workspace.
- Prefer tools for current state, file changes, browser checks, runtime configuration, and hooks.
- Ask the user for more details when a request is underspecified or important assumptions would change the result.
- Self-improvement should be bounded, auditable, and reversible through context files, webhook hooks, runtime config, or explicit code edits.
