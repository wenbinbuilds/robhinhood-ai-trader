import json
import io
import subprocess
import pytest
from agent.collection_telemetry import run_collection
from agent.collection_telemetry import CollectionTelemetry


def test_only_metadata_is_persisted(tmp_path):
    path = tmp_path / "events.jsonl"
    t = CollectionTelemetry(path, {"get_equity_quotes"})
    for kind in ("item.started", "item.completed"):
        t.consume(json.dumps({"type": kind, "item": {
            "id": "private-id", "type": "mcp_tool_call", "tool": "get_equity_quotes",
            "arguments": {"account_number": "sensitive-account"},
            "result": {"access_token": "secret-value"}, "status": "completed"}}))
    data = path.read_text()
    assert "get_equity_quotes" in data
    assert "duration_seconds" in data
    for secret in ("private-id", "sensitive-account", "secret-value", "account_number", "access_token"):
        assert secret not in data


def test_free_text_and_unknown_names_are_not_logged(tmp_path):
    path = tmp_path / "events.jsonl"
    t = CollectionTelemetry(path, set())
    t.consume("a secret non-json line")
    t.consume(json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "secret-tool-name", "text": "private text"}}))
    assert "secret" not in path.read_text()
    assert "private" not in path.read_text()


def test_timeout_diagnostics_do_not_claim_missing_account_or_unattempted_scan():
    from agent.codex_mcp_bridge import diagnostic_lines
    lines = "\n".join(diagnostic_lines({}, bridge_status="CODEX_TIMEOUT"))
    assert "AGENTIC ACCOUNT: NOT_VERIFIED" in lines
    assert "SCANNER: UNKNOWN_COLLECTION_INCOMPLETE" in lines
    assert "POSITIONS: UNKNOWN" in lines


@pytest.mark.parametrize("timed_out", [False, True])
def test_process_timeout_and_metadata_capture_mocked(tmp_path, monkeypatch, timed_out):
    class Process:
        pid = 12345
        returncode = 0
        stdin = io.StringIO()
        stdout = io.StringIO('{"type":"turn.started"}\n')
        stderr = io.StringIO('Authorization: secret\nerror: account 987654321 failed\n')
        def wait(self, timeout=None):
            if timed_out and timeout is not None:
                raise subprocess.TimeoutExpired(["codex", "exec"], timeout)
            return self.returncode
    process = Process()
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("os.killpg", lambda pid, sig: calls.append(pid))
    path = tmp_path / "events.jsonl"
    def run():
        return run_collection(["codex", "exec"], cwd=tmp_path, input_text="fixture", timeout=1,
                              log_path=path, allowed_tools=set())
    if timed_out:
        with pytest.raises(subprocess.TimeoutExpired):
            run()
        assert calls == [12345]
    else:
        assert run().returncode == 0
        assert not calls
    assert "secret" not in path.read_text()
    assert "987654321" not in path.read_text()
    assert "COLLECTION_FINISHED" in path.read_text()


def test_timeout_phase_after_mcp_completion_without_structured_output(tmp_path, monkeypatch):
    class Process:
        pid = 123
        returncode = 0
        stdin = io.StringIO()
        stdout = io.StringIO(
            '{"type":"item.started","item":{"id":"x","type":"mcp_tool_call","tool":"get_equity_quotes"}}\n'
            '{"type":"item.completed","item":{"id":"x","type":"mcp_tool_call","tool":"get_equity_quotes"}}\n'
        )
        stderr = io.StringIO()
        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(["codex", "exec"], timeout)
            return -9
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr("os.killpg", lambda *args: None)
    path = tmp_path / "events.jsonl"
    with pytest.raises(subprocess.TimeoutExpired):
        run_collection(["codex", "exec"], cwd=tmp_path, input_text="fixture", timeout=1,
                       log_path=path, allowed_tools={"get_equity_quotes"}, stage="BENCHMARK",
                       output_path=tmp_path / "missing.json")
    finished = json.loads(path.read_text().splitlines()[-1])
    assert finished["timeout_phase"] == "STRUCTURED_OUTPUT_OR_CODEX_RESPONSE_WAIT"
    assert finished["mcp_calls_started"] == finished["mcp_calls_completed"] == 1
    assert finished["structured_output_first_seen_seconds"] is None
