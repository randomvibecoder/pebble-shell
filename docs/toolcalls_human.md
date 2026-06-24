# Pebble Shell Tool Calls

This document describes every Pebble Shell tool call exposed by `WorkspaceTools`.

Every tool result is returned to the model as a JSON object:

```json
{"ok": true, "output": "..."}
```

or:

```json
{"ok": false, "output": "..."}
```

The `output` field is always a string. Some tools put JSON inside that string; those JSON payload shapes are shown below. Dispatcher-level failures are shared by all tools:

```json
{"ok": false, "output": "Invalid JSON arguments: <json error>"}
{"ok": false, "output": "Missing required argument: '<name>'"}
{"ok": false, "output": "Unknown tool: <name>"}
{"ok": false, "output": "<exception message>"}
```

Paths are workspace-scoped. For foreground tools, relative paths resolve from the workspace root. For background workers, relative paths resolve from the worker folder; a leading `/` means `/workspace`, not container root.

## File And Search Tools

### `ls(path: str = ".", limit: int = 200)`

Lists a file or directory. `limit` is clamped to `1..1000`.

Success for a file:

```json
{"ok": true, "output": "relative/path.txt"}
```

Success for a directory:

```json
{"ok": true, "output": "dir/file.txt\ndir/subdir/"}
```

Success for a truncated directory:

```json
{"ok": true, "output": "dir/file-1.txt\ndir/file-2.txt\n[ls truncated at 2 entries]"}
```

Success for an empty directory:

```json
{"ok": true, "output": "(empty)"}
```

Failures:

```json
{"ok": false, "output": "Path escapes workspace: <path>"}
{"ok": false, "output": "No such path: <path>"}
```

### `glob(pattern: str, path: str = ".", max_results: int = 100)`

Finds files matching a glob pattern. `max_results` is clamped to `1..1000`.

Success with matches:

```json
{"ok": true, "output": "src/a.py\nsrc/b.py"}
```

Success with no matches:

```json
{"ok": true, "output": "(no matches)"}
```

Failures:

```json
{"ok": false, "output": "Path escapes workspace: <path>"}
{"ok": false, "output": "No such path: <path>"}
```

### `grep(pattern: str, path: str = ".", max_results: int = 100)`

Runs `rg --line-number --color never --max-count <max_results> <pattern> <path>`. `max_results` is clamped to `1..1000`.

Success with matches:

```json
{"ok": true, "output": "/workspace/file.py:12:matched text"}
```

Success with no matches:

```json
{"ok": true, "output": "(no matches)"}
```

Success when output lines exceed `max_results`:

```json
{"ok": true, "output": "<first max_results lines>\n[grep results truncated]"}
```

Failures:

```json
{"ok": false, "output": "Path escapes workspace: <path>"}
{"ok": false, "output": "No such path: <path>"}
{"ok": false, "output": "<rg stderr or rg exited N>"}
```

### `read(path: str)`

Reads a UTF-8 text file. Reads at most 40,000 bytes/chars into context.

Success:

```json
{"ok": true, "output": "<file text>"}
```

Success when truncated:

```json
{"ok": true, "output": "<first content>\n[read truncated at 40000 bytes/chars. Use targeted shell commands such as sed, rg, head, tail, wc, or file-specific extractors to inspect the remaining content.]"}
```

Failures:

```json
{"ok": false, "output": "Path escapes workspace: <path>"}
{"ok": false, "output": "Not a file: <path>"}
{"ok": false, "output": "Refusing to read likely binary file <relative-path> into model context. Use a purpose-built extractor/converter or shell command that returns a small text excerpt."}
```

### `write(path: str, content: str)`

Writes UTF-8 text to a file, creating parent directories.

Success:

```json
{"ok": true, "output": "Wrote <byte_count> bytes to <relative-path>"}
```

Failure:

```json
{"ok": false, "output": "Path escapes workspace: <path>"}
```

### `edit(path: str, old: str, new: str, replace_all: bool = false)`

Replaces exact text in a UTF-8 file.

Success:

```json
{"ok": true, "output": "Edited <relative-path> with <replacement_count> replacement(s)"}
```

Failures:

```json
{"ok": false, "output": "old text cannot be empty"}
{"ok": false, "output": "Path escapes workspace: <path>"}
{"ok": false, "output": "Not a file: <path>"}
{"ok": false, "output": "Refusing to edit likely binary file <relative-path>"}
{"ok": false, "output": "old text not found in <relative-path>"}
{"ok": false, "output": "old text occurs <count> times; set replace_all=true or provide a more specific old string"}
```

### `patch(patch: str)`

Applies a Codex-style patch with `*** Begin Patch` and `*** End Patch`.

Success:

```json
{"ok": true, "output": "added <path>\nupdated <path>\ndeleted <path>"}
```

Success with no changes:

```json
{"ok": true, "output": "Patch had no changes"}
```

Failures:

```json
{"ok": false, "output": "Cannot add existing file: <path>"}
{"ok": false, "output": "Cannot delete missing file: <path>"}
{"ok": false, "output": "Cannot update missing file: <path>"}
{"ok": false, "output": "Refusing to patch likely binary file <relative-path>"}
{"ok": false, "output": "Unsupported patch change kind: <kind>"}
{"ok": false, "output": "<patch parse or apply error>"}
```

### `read_image(path: str, question: str = "Describe this image.")`

Inspects a local image with the configured OpenAI-compatible model chain. Supported suffixes: `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`.

Success:

```json
{"ok": true, "output": "<model answer>"}
```

Failures:

```json
{"ok": false, "output": "read_image requires OPENAI_API_KEY"}
{"ok": false, "output": "Path escapes workspace: <path>"}
{"ok": false, "output": "Not a file: <path>"}
{"ok": false, "output": "Unsupported image type: <suffix>"}
{"ok": false, "output": "Image exceeds <max_bytes> bytes: <relative-path>"}
{"ok": false, "output": "All configured OpenAI-compatible image inspection models failed: <model>: <error> | <model>: <error>"}
```

## Shell And Process Tools

### `bash(command: str)`

Runs a shell command with `/bin/bash`, waits up to the configured shell timeout, and returns combined stdout/stderr. The command is audited when shell audit is enabled.

Success with output:

```json
{"ok": true, "output": "<stdout and stderr>"}
```

Success with no output:

```json
{"ok": true, "output": "exit code 0"}
```

Failure from nonzero exit:

```json
{"ok": false, "output": "<stdout and stderr or exit code N>"}
```

Failure from invalid command:

```json
{"ok": false, "output": "Invalid shell command: <error>"}
```

If combined output exceeds 50,000 characters:

```json
{"ok": true, "output": "<first 50000 chars>\n[bash output truncated at 50000 chars. Full stdout/stderr saved at /tmp/pebble_tool_output_<random>.log; use bash commands such as sed, rg, head, tail, wc, or cat on that file to inspect specific parts.]"}
```

The same truncation format is used when `ok` is false.

### `exec_command(cmd: str, login: bool = true, yield_time_ms: int = 10000, max_output_tokens: int = 20000, shell: str | null = null, tty: bool = false, workdir: str | null = null)`

Starts a terminal session. If it is still running after `yield_time_ms`, return the session status and poll it later with `write_stdin`.

Success output is a JSON string:

```json
{
  "command": "<cmd>",
  "log_file": ".pebble_shell/terminal_sessions/session_<id>.log",
  "output": "<tail of output>",
  "pid": 123,
  "returncode": null,
  "running": true,
  "session_id": 1,
  "started_at": 1710000000.0,
  "tty": false
}
```

If the process already exited, `running` is `false` and `returncode` is an integer.

Failures:

```json
{"ok": false, "output": "Invalid process command: <error>"}
{"ok": false, "output": "Path escapes workspace: <workdir>"}
{"ok": false, "output": "workdir escapes workspace: <resolved-path>"}
{"ok": false, "output": "cmd cannot be empty"}
```

### `write_stdin(session_id: int, chars: str = "", yield_time_ms: int = 10000, max_output_tokens: int = 20000)`

Writes to or polls an `exec_command` session. Use `chars=""` to poll without input.

Success output is the same JSON status shape as `exec_command`.

Failures:

```json
{"ok": false, "output": "Invalid session_id: <value>"}
{"ok": false, "output": "Unknown session_id: <id>"}
{"ok": false, "output": "Session <id> does not accept stdin"}
```

### `shell_audit(limit: int = 20)`

Lists recent shell audit records. `limit` is clamped to `1..50`.

Success output is a JSON array:

```json
[
  {
    "allowed": true,
    "command": "ls",
    "created_at": "YYYY-MM-DD HH:MM:SS",
    "exit_code": 0,
    "output": "<captured output, truncated to 4000 chars in DB>",
    "reason": "Allowed inside Docker container",
    "risk": "normal"
  }
]
```

Failure:

```json
{"ok": false, "output": "Shell audit store is not enabled"}
```

## Web And User Delivery

### `websearch(query: str, num_results: int = 5)`

Searches Exa. `num_results` is clamped to `1..10`.

Success output is the JSON response from Exa:

```json
{"results": [{"title": "...", "url": "..."}]}
```

Failures:

```json
{"ok": false, "output": "EXA_API_KEY is required for websearch"}
{"ok": false, "output": "Exa query cannot be empty"}
{"ok": false, "output": "Exa search failed: HTTP <code>: <body>"}
```

### `send_file(path: str)`

Sends a file to the user when a file sender is configured.

Success with sender:

```json
{"ok": true, "output": "Sent <relative-path> to the user"}
```

The configured sender may return a custom success string instead.

Success without sender:

```json
{"ok": true, "output": "File ready at <relative-path>; no file sender is configured"}
```

Failures:

```json
{"ok": false, "output": "Path escapes workspace: <path>"}
{"ok": false, "output": "Not a file: <path>"}
{"ok": false, "output": "File exceeds <max_bytes> bytes: <relative-path>"}
{"ok": false, "output": "File send failed for <relative-path>: <error>"}
```

### `send_msg(msg: str)`

Sends a short progress message. Final assistant replies should not use this tool.

Success with sender:

```json
{"ok": true, "output": "Sent progress message to the user"}
```

The configured sender may return a custom success string instead.

Success without sender:

```json
{"ok": true, "output": "Progress message ready; no text sender is configured"}
```

Failures:

```json
{"ok": false, "output": "send_msg requires a non-empty msg"}
{"ok": false, "output": "send_msg msg must be 500 characters or fewer"}
```

## Runtime Config Tools

### `get_runtime_config()`

Reads persisted runtime settings.

Success output is a JSON object containing any stored supported keys:

```json
{
  "heartbeat_every_seconds": "7200",
  "openai_model": "model/name"
}
```

Failure:

```json
{"ok": false, "output": "Runtime config store is not enabled"}
```

### `set_runtime_config(key: str, value: str)`

Persists a runtime setting. Supported keys are `openai_model` and `heartbeat_every_seconds`.

Success:

```json
{"ok": true, "output": "Set <key>=<value>"}
```

Failures:

```json
{"ok": false, "output": "Runtime config store is not enabled"}
{"ok": false, "output": "Unsupported runtime config key: <key>"}
{"ok": false, "output": "heartbeat_every_seconds must be >= 0"}
{"ok": false, "output": "openai_model cannot be empty"}
```

## Event Hook Tools

Hook names must be `1..64` characters and contain only letters, numbers, underscores, or hyphens.

### `hook_set(name: str, prompt: str)`

Creates or updates a local webhook hook.

Success:

```json
{"ok": true, "output": "Saved hook <name>; POST /webhooks/<name> records a local event and returns an event id/status immediately. It does not return the agent result. Use an adapter-specific CLI/API for replies to external systems. If API auth is enabled, backend callers should read the bearer token at runtime from /workspace/.pebble_shell/secrets/api_auth_token."}
```

Failures:

```json
{"ok": false, "output": "Event hook store is not enabled"}
{"ok": false, "output": "name must be 1-64 chars and contain only letters, numbers, underscores, or hyphens"}
{"ok": false, "output": "hook prompt cannot be empty"}
```

### `hook_list(limit: int = 20)`

Lists registered hooks. `limit` is clamped to `1..50`.

Success output is a JSON array:

```json
[
  {
    "enabled": true,
    "name": "suggestions",
    "prompt": "Handle suggestion payloads.",
    "updated_at": "YYYY-MM-DD HH:MM:SS"
  }
]
```

Failure:

```json
{"ok": false, "output": "Event hook store is not enabled"}
```

### `hook_show(name: str)`

Shows one hook.

Success output is a JSON object:

```json
{
  "enabled": true,
  "name": "suggestions",
  "prompt": "Handle suggestion payloads.",
  "updated_at": "YYYY-MM-DD HH:MM:SS"
}
```

Failures:

```json
{"ok": false, "output": "Event hook store is not enabled"}
{"ok": false, "output": "Unknown hook: <name>"}
```

### `hook_enable(name: str)` and `hook_disable(name: str)`

Enables or disables a hook.

Success:

```json
{"ok": true, "output": "Set hook <name> enabled=True"}
{"ok": true, "output": "Set hook <name> enabled=False"}
```

Failures:

```json
{"ok": false, "output": "Event hook store is not enabled"}
{"ok": false, "output": "name must be 1-64 chars and contain only letters, numbers, underscores, or hyphens"}
{"ok": false, "output": "Unknown hook: <name>"}
```

### `hook_remove(name: str)`

Deletes a hook. Event history remains.

Success:

```json
{"ok": true, "output": "Removed hook <name>; existing event history was kept"}
```

Failures:

```json
{"ok": false, "output": "Event hook store is not enabled"}
{"ok": false, "output": "name must be 1-64 chars and contain only letters, numbers, underscores, or hyphens"}
{"ok": false, "output": "Unknown hook: <name>"}
```

### `hook_events(limit: int = 20)`

Lists recent webhook events. `limit` is clamped to `1..50`.

Success output is a JSON array:

```json
[
  {
    "background": true,
    "created_at": "YYYY-MM-DD HH:MM:SS",
    "error": null,
    "id": 1,
    "name": "suggestions",
    "payload": {"key": "value"},
    "processed_at": null,
    "result_excerpt": null,
    "status": "received"
  }
]
```

Failure:

```json
{"ok": false, "output": "Event hook store is not enabled"}
```

### `hook_event_replay(event_id: int)`

Schedules a replay of a prior webhook event.

Success:

```json
{"ok": true, "output": "<webhook replay scheduler output>"}
```

Failures:

```json
{"ok": false, "output": "Event hook store is not enabled"}
{"ok": false, "output": "Webhook replay scheduler is not enabled"}
{"ok": false, "output": "Unknown webhook event: <event_id>"}
```

## Cron Tools

### `cron_job_save(name: str, prompt: str, every_seconds: int, enabled: bool = true)`

Creates or updates a scheduled job. `every_seconds` must be at least 60.

Success:

```json
{"ok": true, "output": "Saved cron job <name> every <every_seconds> seconds"}
```

Failures:

```json
{"ok": false, "output": "Cron store is not enabled"}
{"ok": false, "output": "name must be 1-64 chars and contain only letters, numbers, underscores, or hyphens"}
{"ok": false, "output": "cron every_seconds must be at least 60"}
{"ok": false, "output": "cron prompt cannot be empty"}
```

### `cron_list(jobs_limit: int = 20, runs_limit: int = 20)`

Lists scheduled jobs and recent runs. `jobs_limit` and `runs_limit` are clamped to `1..50`.

Success output is a JSON object:

```json
{
  "jobs": [
    {
      "enabled": true,
      "every_seconds": 3600,
      "last_run_at": null,
      "name": "hourly",
      "next_run_at": 1710000000.0,
      "prompt": "Do the scheduled task.",
      "updated_at": "YYYY-MM-DD HH:MM:SS"
    }
  ],
  "runs": [
    {
      "content": "Agent response.",
      "created_at": "YYYY-MM-DD HH:MM:SS",
      "job_name": "hourly",
      "ok": true,
      "steps": 2
    }
  ]
}
```

Failure:

```json
{"ok": false, "output": "Cron store is not enabled"}
```

### `cron_enable(name: str, enabled: bool)`

Pauses or resumes a scheduled job.

Success:

```json
{"ok": true, "output": "Set cron job <name> enabled=<enabled>"}
```

Failures:

```json
{"ok": false, "output": "Cron store is not enabled"}
{"ok": false, "output": "Unknown cron job: <name>"}
```

## Subagent Tools

These tools require the background task service. If it is missing, they return:

```json
{"ok": false, "output": "Background task service is not enabled"}
```

Several tools require the background loop. If it is missing, they return:

```json
{"ok": false, "output": "Background task runner is not attached to a running event loop"}
```

Subagent status values include `running`, `pausing`, `paused`, `blocked`, `completed`, `cancelling`, and `canceled`.

### `subagent_start(prompt: str, folder: str)`

Starts a background worker. The folder must remain inside `/workspace` and cannot contain `.`, `..`, or empty path segments.

Success output is a JSON object like `subagent_status`:

```json
{
  "attention_summary": "",
  "completion_tokens": null,
  "created_at": 1710000000.0,
  "error": "",
  "events": [
    {
      "created_at": "YYYY-MM-DD HH:MM:SS",
      "id": 1,
      "kind": "running",
      "message": "Background worker created and scheduled.",
      "payload": {}
    }
  ],
  "finished_at": null,
  "folder": "worker-folder",
  "id": "bg_YYYYMMDD_ab12cd",
  "last_model": "",
  "model_calls": 0,
  "prompt": "Worker task prompt.",
  "prompt_tokens": null,
  "result": "",
  "self_check_retries": 0,
  "started_at": null,
  "status": "running",
  "steps": 0,
  "total_tokens": null,
  "updated_at": 1710000000.0
}
```

Failures:

```json
{"ok": false, "output": "Background task folder cannot be empty"}
{"ok": false, "output": "background task folder must stay inside /workspace and cannot contain . or .. path segments"}
{"ok": false, "output": "Maximum active background tasks reached (<max>)"}
```

### `subagent_status(job_id: str)`

Shows one worker, including prompt and recent events.

Success output is the same JSON object shape as `subagent_start`.

Failure:

```json
{"ok": false, "output": "Unknown background job: <job_id>"}
```

### `subagent_list(limit: int = 10, status: str | null = null)`

Lists recent workers. `limit` is clamped to `1..100`.

Success output is a JSON array:

```json
[
  {
    "attention_summary": "",
    "completion_tokens": 20,
    "created_at": 1710000000.0,
    "error": "",
    "finished_at": null,
    "folder": "worker-folder",
    "id": "bg_YYYYMMDD_ab12cd",
    "last_model": "model/name",
    "model_calls": 1,
    "prompt_tokens": 100,
    "result": "",
    "self_check_retries": 0,
    "started_at": 1710000001.0,
    "status": "running",
    "steps": 3,
    "total_tokens": 120,
    "updated_at": 1710000002.0
  }
]
```

### `subagent_dashboard(limit: int = 10, status: str | null = null)`

Returns a cheap multi-worker dashboard from stored state only. It does not call an LLM. `limit` is clamped to `1..100`. `recent_activity` contains the newest 5 stored events, with each message clipped to 400 characters.

Success output is a JSON object:

```json
{
  "jobs": [
    {
      "elapsed": "1m 2s",
      "flags": [],
      "job_id": "bg_YYYYMMDD_ab12cd",
      "model": "model/name",
      "model_calls": 4,
      "recent_activity": [
        {
          "message": "write: ok",
          "time": "YYYY-MM-DD HH:MM:SS UTC"
        }
      ],
      "status": "running",
      "steps": 3,
      "suspicious_completion": false,
      "tokens": {
        "completion": 20,
        "prompt": 100,
        "total": 120
      },
      "tool_calls": 2
    }
  ]
}
```

Possible `flags` include `early-complete`, `retry-cap`, `blocked-text`, and `stale-api`.

### `subagent_summary(job_id: str)`

Returns a richer one-worker status. It may call the flash model. The flash `recent_activity` is a single paragraph capped at 1000 characters. If no worker events or context changed since the previous summary, the cached paragraph is reused. If flash fails, fallback uses stored events/results.

Success output is a JSON object:

```json
{
  "elapsed": "1m 2s",
  "events": [
    {
      "created_at": "YYYY-MM-DD HH:MM:SS",
      "id": 1,
      "kind": "tool_call",
      "message": "write: ok",
      "payload": {}
    }
  ],
  "flags": [],
  "job_id": "bg_YYYYMMDD_ab12cd",
  "model": "model/name",
  "model_calls": 4,
  "recent_activity": "One paragraph summary up to 1000 characters.",
  "status": "running",
  "steps": 3,
  "summary_source": "flash",
  "suspicious_completion": false,
  "tokens": {
    "completion": 20,
    "prompt": 100,
    "total": 120
  },
  "tool_calls": 2
}
```

If flash fails:

```json
{
  "summary_source": "fallback",
  "recent_activity": "Done: <stored result or event>. Now: <status>."
}
```

If the cached summary is reused, `summary_source` is the original cached source, usually `flash` or `fallback`.

Failure:

```json
{"ok": false, "output": "Unknown background job: <job_id>"}
```

### `subagent_ask(job_id: str, question: str)`

Asks a no-tools model question over the worker's stored context.

Success:

```json
{"ok": true, "output": "<model answer>"}
```

Failures:

```json
{"ok": false, "output": "Unknown background job: <job_id>"}
{"ok": false, "output": "background task question cannot be empty"}
```

### `subagent_cancel(job_id: str)`

Requests cooperative cancellation.

Success output is the same JSON object shape as `subagent_status`, with status updated to `cancelling` unless already terminal.

Failure:

```json
{"ok": false, "output": "Unknown background job: <job_id>"}
```

### `subagent_pause(job_id: str)`

Requests cooperative pause.

Success output is the same JSON object shape as `subagent_status`, with status updated to `pausing` unless already terminal.

Failure:

```json
{"ok": false, "output": "Unknown background job: <job_id>"}
```

### `subagent_send(job_id: str, message: str)`

Queues a new instruction for a running, paused, blocked, pausing, or completed worker. Completed workers are reopened as `running`.

Success:

```json
{"ok": true, "output": "Queued message for background job <job_id>"}
```

Failures:

```json
{"ok": false, "output": "Unknown background job: <job_id>"}
{"ok": false, "output": "Background job is not messageable: <job_id> has status <status>"}
{"ok": false, "output": "background task message cannot be empty"}
```

### `subagent_delete(job_id: str)`

Deletes an inactive worker's records, queued messages, events, and stored context. Active workers must be paused or canceled first.

Success:

```json
{"ok": true, "output": "Finished cleanup for background job <job_id>; deleted job records and stored context."}
```

Failures:

```json
{"ok": false, "output": "Unknown background job: <job_id>"}
{"ok": false, "output": "Background job <job_id> is <status>; pause or cancel it before destructive finish cleanup."}
```

### `subagent_events(job_id: str, limit: int = 20)`

Lists stored events for one worker. `limit` is clamped to `1..100`.

Success output is a JSON array:

```json
[
  {
    "created_at": "YYYY-MM-DD HH:MM:SS",
    "id": 1,
    "kind": "tool_call",
    "message": "write: ok",
    "payload": {}
  }
]
```

Failure:

```json
{"ok": false, "output": "Unknown background job: <job_id>"}
```
