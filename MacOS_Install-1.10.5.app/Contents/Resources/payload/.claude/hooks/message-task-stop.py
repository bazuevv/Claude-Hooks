"""Persist task timing and billing baselines independently of an open webview."""
import json
import os
import sys

import message_timestamps
import message_costs
import cpu_probe


def main():
    data = json.load(sys.stdin)
    event = data.get("hook_event_name")
    if event not in ("UserPromptSubmit", "Stop"):
        return
    transcript = data.get("transcript_path")
    if not isinstance(transcript, str) or (event == "Stop" and not os.path.isfile(transcript)):
        return
    state_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks-runtime")
    cpu_probe.note_event(transcript, 'stop' if event == 'Stop' else 'start')
    if event == "Stop":
        message_timestamps.record_stop(transcript, state_dir)
    else:
        message_costs.record_submission(transcript, state_dir, data.get("prompt"))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError) as exc:
        print("[message-task-stop] " + str(exc), file=sys.stderr)
