# Claude → Codex history recovery

Large histories are archived and reduced **before** `thread/start`, including
messages that become `developerInstructions`. Building permanent instructions
from the full transcript defeats subsequent Codex compaction.

Recovery retains the latest saved summary and a bounded recent tail. The latest
host plan-mode, prompt-hook and environment snapshots have reserved space, even
if they precede the tail. Older versions stay in the archive. User/tool text is
never promoted to host instructions. Oversized summaries or current host state
fail explicitly instead of being silently truncated.

On the first request after an upgrade, a legacy checkpoint with an oversized host
instruction history is rebuilt from the fresh Claude payload. The old checkpoint
remains until the new thread is checkpointed. Checkpoint `recovery_version: 2`
prevents repeating this migration after later restarts. Original Codex rollouts
are not edited or deleted.

Diagnostics under `.claude/hooks-runtime`:

- `recovered-history/*.json`: full original message arrays, including images.
- `bridge-recovery-events.jsonl`: instruction/prompt character counts before and
  after recovery, thread ID and hashed Claude session ID; no message text.
- `codex-compactions.jsonl`: one start/completion pair per compaction item.
- `codex-context-events.jsonl`: UI history; `context_before` is model input usage
  saved at compaction start, and `context_after` is the first real input usage
  after completion. Synthetic zero-usage notifications are ignored. The after
  value is the next request's context, not the encoded summary's size.

`python -B repair_compaction_history.py` previews repairs to old UI history from
adjacent real usage records in local Codex rollouts. `--apply` backs up the log
and appends corrected records, retaining legacy IDs where needed. Repeating it
is idempotent. Ambiguous event matches are skipped.

Focused checks:

```powershell
python -B -m unittest test_bridge_recovery test_bridge_persistence.PersistenceTests test_compaction_audit.AuditTests test_plan_transition test_repair_compaction_history
```

`python -B check-history-recovery.py` optionally checks the full bridge against
GPT-6 Astra using generated synthetic history. It reads no personal chat archive
and executes no host tools. Its results and telemetry are isolated in a
`hooks-runtime/recovery-probe-*` directory.
