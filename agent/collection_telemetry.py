"""Bounded metadata-only CLI telemetry. Raw prompts/results are never persisted."""
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

from watcher.storage import event


class CollectionTelemetry:
    def __init__(self, path, allowed_tools, stage="FULL"):
        self.path, self.allowed_tools = path, set(allowed_tools)
        self.stage = stage
        self.started = time.monotonic()
        self.run_id = uuid.uuid4().hex
        self.items = {}
        self.first_mcp_at = None
        self.last_mcp_completed_at = None
        self.mcp_calls_started = 0
        self.mcp_calls_completed = 0
        self.tool_intervals = []
        self.event_counts = Counter()
        self.structured_output_first_seen_at = None
        self.structured_output_bytes = 0

    def record(self, kind, **fields):
        event(self.path, kind, datetime.now(timezone.utc), run_id=self.run_id, stage=self.stage,
              elapsed_seconds=round(time.monotonic() - self.started, 3), **fields)

    def consume(self, line):
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(value, dict):
            return
        kind = value.get("type")
        if not isinstance(kind, str) or kind not in {"thread.started", "turn.started", "turn.completed", "turn.failed", "error",
                        "item.started", "item.updated", "item.completed"}:
            return
        self.event_counts[kind] += 1
        item = value.get("item", {})
        if not isinstance(item, dict):
            item = {}
        category = item.get("type")
        if not isinstance(category, str) or category not in {"mcp_tool_call", "web_search", "command_execution", "agent_message", "reasoning", "file_change", "plan"}:
            category = "OTHER"
        fields = {"item_type": category}
        tool = item.get("tool")
        if isinstance(tool, str) and tool in self.allowed_tools:
            fields["tool"] = tool
        # Nested code-mode calls can bundle MCP tools. Record only allowlisted
        # names mentioned, never arguments, code, outputs, server IDs or secrets.
        arguments = str(item.get("arguments", ""))
        mentioned = sorted(name for name in self.allowed_tools if name in arguments)
        if mentioned:
            fields["mentioned_tools"] = mentioned
        identity = item.get("id")
        if category == "mcp_tool_call" and kind == "item.started":
            self.first_mcp_at = self.first_mcp_at or time.monotonic()
            self.mcp_calls_started += 1
        if category == "mcp_tool_call" and kind == "item.completed":
            self.last_mcp_completed_at = time.monotonic()
            self.mcp_calls_completed += 1
        if isinstance(identity, str):
            if kind == "item.started":
                self.items[identity] = (time.monotonic(), fields.get("tool"))
            elif kind == "item.completed" and identity in self.items:
                item_started, item_tool = self.items.pop(identity)
                item_finished = time.monotonic()
                fields["duration_seconds"] = round(item_finished - item_started, 3)
                if item_tool:
                    self.tool_intervals.append((item_tool, item_started, item_finished))
        status = item.get("status")
        if isinstance(status, str) and status in {"in_progress", "completed", "failed"}:
            fields["status"] = status
        if item.get("error"):
            fields["tool_error"] = True
        self.record(kind, **fields)


def run_collection(command, *, cwd, input_text, timeout, log_path, allowed_tools, stage="FULL", output_path=None):
    telemetry = CollectionTelemetry(log_path, allowed_tools, stage)
    telemetry.record("COLLECTION_STARTED", timeout_seconds=timeout,
                     prompt_bytes=len((input_text or "").encode("utf-8")))
    errors = []
    reader_errors = []
    timeout_phase = None
    timed_out = False
    raised_exception = None
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)

    def read_stdout():
        try:
            for line in process.stdout:
                telemetry.consume(line)
        except Exception as exc:
            reader_errors.append(type(exc).__name__)

    def read_stderr():
        for line in process.stderr:
            # Classification only: even an error message can echo an account
            # number or a prompt. Never retain arbitrary stderr text here.
            lowered = line.lower()
            categories = (
                ("invalid_json_schema", "INVALID_JSON_SCHEMA"),
                ("unauthorized", "authentication required"),
                ("authentication required", "authentication required"),
                ("rate limit", "RATE_LIMITED"),
                ("failed to connect", "connection failed"),
                ("error", "ERROR_REPORTED"),
            )
            for marker, category in categories:
                if marker in lowered and category not in errors:
                    errors.append(category)

    def write_input():
        try:
            process.stdin.write(input_text or "")
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    stop_monitor = threading.Event()

    def monitor_output():
        if output_path is None:
            return
        while not stop_monitor.wait(0.02):
            try:
                size = os.path.getsize(output_path)
            except OSError:
                continue
            telemetry.structured_output_bytes = size
            if telemetry.structured_output_first_seen_at is None:
                telemetry.structured_output_first_seen_at = time.monotonic()

    threads = [threading.Thread(target=f) for f in (read_stdout, read_stderr, write_input, monitor_output)]
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=timeout)
    except BaseException as exc:
        raised_exception = exc
        if isinstance(exc, subprocess.TimeoutExpired):
            timed_out = True
        # Kill only the new process group owned by THIS invocation, including
        # the CLI wrapper's child. Never leave a collector running after timeout.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        telemetry.record("COLLECTION_INTERRUPTED", timeout=time.monotonic() - telemetry.started >= timeout)
        raise
    finally:
        stop_monitor.set()
        for thread in threads:
            thread.join()
        if timed_out:
            if any(value[1] is not None for value in telemetry.items.values()):
                timeout_phase = "MCP_CALL_IN_PROGRESS"
            elif telemetry.structured_output_first_seen_at is not None:
                timeout_phase = "PROCESS_EXIT_AFTER_STRUCTURED_OUTPUT"
            elif telemetry.last_mcp_completed_at is not None:
                timeout_phase = "STRUCTURED_OUTPUT_OR_CODEX_RESPONSE_WAIT"
            elif telemetry.first_mcp_at is not None:
                timeout_phase = "BETWEEN_MCP_CALLS_OR_CODEX_REASONING"
            else:
                timeout_phase = "CODEX_STARTUP_OR_PRE_MCP_REASONING"
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
        finished = time.monotonic()
        telemetry.record(
            "COLLECTION_FINISHED", returncode=process.returncode,
            diagnostic_stderr="\n".join(errors), telemetry_errors=reader_errors,
            mcp_calls_started=telemetry.mcp_calls_started,
            mcp_calls_completed=telemetry.mcp_calls_completed,
            time_to_first_mcp_call_seconds=(
                round(telemetry.first_mcp_at - telemetry.started, 3)
                if telemetry.first_mcp_at is not None else None
            ),
            # Codex JSONL has no dedicated MCP-handshake event. Recording null
            # is more honest than guessing from the first tool-call timestamp.
            mcp_connection_time_seconds=None,
            final_response_or_process_exit_wait_seconds=(
                round(finished - telemetry.last_mcp_completed_at, 3)
                if telemetry.last_mcp_completed_at is not None else None
            ),
            structured_output_first_seen_seconds=(
                round(telemetry.structured_output_first_seen_at - telemetry.started, 3)
                if telemetry.structured_output_first_seen_at is not None else None
            ),
            structured_output_bytes=telemetry.structured_output_bytes,
            stdout_event_counts=dict(telemetry.event_counts),
            timeout_phase=timeout_phase,
        )
        telemetry_summary = {
        "mcp_connection_time_seconds": None,
        "time_to_first_mcp_call_seconds": (
            telemetry.first_mcp_at - telemetry.started if telemetry.first_mcp_at is not None else None
        ),
        "final_response_or_process_exit_wait_seconds": (
            finished - telemetry.last_mcp_completed_at if telemetry.last_mcp_completed_at is not None else None
        ),
        "mcp_calls_started": telemetry.mcp_calls_started,
        "mcp_calls_completed": telemetry.mcp_calls_completed,
        "tool_intervals": telemetry.tool_intervals,
        "structured_output_first_seen_seconds": (
            telemetry.structured_output_first_seen_at - telemetry.started
            if telemetry.structured_output_first_seen_at is not None else None
        ),
        "structured_output_bytes": telemetry.structured_output_bytes,
        "stdout_event_counts": dict(telemetry.event_counts),
        "timeout_phase": timeout_phase,
        }
        if raised_exception is not None:
            raised_exception.telemetry_summary = telemetry_summary
    completed = subprocess.CompletedProcess(command, process.returncode, "", "\n".join(errors))
    completed.telemetry_summary = telemetry_summary
    return completed
