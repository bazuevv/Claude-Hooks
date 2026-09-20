"""Live synthetic plan approval check; host tools are simulated, never executed."""
import argparse
import json
import time
import uuid
from pathlib import Path

from codex_anthropic_bridge import CodexTextBackend, collect_message


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expire-turn", action="store_true",
                        help="End the Codex turn while the simulated host is approving the plan.")
    args = parser.parse_args()
    run = "plan-probe-" + uuid.uuid4().hex
    runtime = Path(__file__).resolve().parent.parent / "hooks-runtime" / run
    runtime.mkdir()
    settings = json.loads((Path.home() / ".claude/settings_openai.json").read_text(encoding="utf-8"))
    payload = {
        "model": settings["env"]["ANTHROPIC_MODEL"],
        "output_config": {"effort": "low"},
        "metadata": {"session_id": run},
        "system": "Synthetic test. Use only the supplied dynamic tools. Follow current host mode notices.",
        "tools": [{"name": name, "description": description,
                   "input_schema": {"type": "object", "properties": {}}}
                  for name, description in [
                      ("ExitPlanMode", "Ask the host to approve the plan."),
                      ("ImplementationProbe", "Simulate implementation. No files are changed."),
                  ]],
        "messages": [
            {"role": "user", "content": "Plan then execute the simulated ImplementationProbe task."},
            {"role": "system", "content": "Plan mode is active. Call ExitPlanMode to request approval. Do not implement until the host exits plan mode."},
        ],
    }
    backend = CodexTextBackend(timeout=180, usage_state_file=str(runtime / "usage.json")).start()
    try:
        first = collect_message(backend.begin(payload))
        calls = [b for b in first["content"] if b["type"] == "tool_use"]
        assert len(calls) == 1 and calls[0]["name"] == "ExitPlanMode", first
        print("PASS: requested plan approval", flush=True)
        session = backend._sessions[run]
        thread = session.thread_id
        previous_turn = session.active_turn_id
        if args.expire_turn:
            backend.client.request("turn/interrupt", {
                "threadId": thread, "turnId": previous_turn,
            })
            # Keep old notifications queued to exercise recovery's event filter.
            drained = []
            deadline = time.monotonic() + 30
            while True:
                event = session.events.get(timeout=max(0.001, deadline - time.monotonic()))
                drained.append(event)
                if event.get("method") == "turn/completed":
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Interrupted probe turn did not complete")
            for event in drained:
                session.events.put(event)
            print("PASS: ended the old turn before delivering approval", flush=True)
        payload["messages"] += [
            {"role": "assistant", "content": first["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls[0]["id"],
             "content": "User has approved your plan. You can now start coding. Approved plan: call ImplementationProbe."}]},
            {"role": "system", "content": [{"type": "text", "text":
             "## Exited Plan Mode\nYou have exited plan mode. You can now make edits, run tools, and take actions."}]},
        ]
        second = collect_message(backend.begin(payload))
        calls = [b for b in second["content"] if b["type"] == "tool_use"]
        assert len(calls) == 1 and calls[0]["name"] == "ImplementationProbe", second
        assert backend._sessions[run].thread_id == thread
        if args.expire_turn:
            assert session.active_turn_id != previous_turn
        print("PASS: approval continued to implementation in the same thread", flush=True)
        payload["messages"] += [
            {"role": "assistant", "content": second["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls[0]["id"],
             "content": "Synthetic implementation complete. Reply DONE."}]},
        ]
        final = collect_message(backend.begin(payload))
        assert final["stop_reason"] == "end_turn", final
        print("PASS: finished after simulated implementation", flush=True)
    finally:
        backend.close()


if __name__ == "__main__":
    main()
