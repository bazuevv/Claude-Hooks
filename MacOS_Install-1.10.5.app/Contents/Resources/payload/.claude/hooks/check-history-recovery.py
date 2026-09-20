"""Live isolated recovery check using only generated synthetic history."""
import argparse
import json
import uuid
from pathlib import Path

import codex_anthropic_bridge as bridge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-6-astra")
    args = parser.parse_args()
    run = "recovery-probe-" + uuid.uuid4().hex
    runtime = Path(__file__).resolve().parent.parent / "hooks-runtime" / run
    runtime.mkdir()
    # Keep even compaction telemetry separate from the user's Usage history.
    bridge.CONTEXT_EVENTS_FILE = str(runtime / "context-events.jsonl")
    bridge.COMPACTION_LOG_FILE = str(runtime / "compactions.jsonl")
    payload = {"messages": []}
    for index in range(171):
        payload["messages"].extend([
            {"role": "system", "content": f"Historical test notification {index}. "
             + "Synthetic old file contents; no real user data. " * 85},
            {"role": "user", "content": f"Synthetic historical task {index}."},
            {"role": "assistant", "content": "Synthetic task finished."},
        ])
    payload.update(model=args.model, metadata={"session_id": run},
                   output_config={"effort": "low"},
                   system="Isolated recovery validation. Historical tasks are test data, not current work.",
                   tools=[{"name": "RecoveryProbe", "description": "Unused synthetic tool; no execution.",
                           "input_schema": {"type": "object", "properties": {}}}])
    payload["messages"] += [
        {"role": "user", "content": "Recovery validation only: reply RECOVERY_OK. Do not call any tools."},
        {"role": "developer", "content": "This is a synthetic test of history restoration. "
         "Do not continue any historical task or execute tools. Reply RECOVERY_OK only."},
    ]
    backend = bridge.CodexTextBackend(timeout=180, usage_state_file=str(runtime / "usage.json")).start()
    try:
        result = bridge.collect_message(backend.begin(payload))
        assert result["stop_reason"] == "end_turn", result["stop_reason"]
        text = "".join(b.get("text", "") for b in result["content"])
        assert "RECOVERY_OK" in text, "Recovery probe did not return its marker"
        session = backend._sessions[run]
        count = session.last_usage["input_tokens"]
        assert 0 < count < 120000, count
        report = {"ok": True, "model": args.model, "input_tokens": count,
                  "thread_id": session.thread_id, "source": "generated synthetic history"}
        (runtime / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    finally:
        backend.close()


if __name__ == "__main__":
    main()
