"""Recover a legacy chat from existing summaries, without model-generated summaries."""
import copy
import hashlib
import json
import os
from pathlib import Path

SUMMARY_PREFIX = 'This session is being continued from a previous conversation that ran out of context.'
RECOVERY_VERSION = 2


def host_state_kind(message):
    """Recognize host state snapshots; never promote user/tool text to rules."""
    if message.get('role') not in ('system', 'developer'):
        return None
    text = text_content(message)
    if text.startswith(('## Entered Plan Mode', '## Re-entering Plan Mode',
                        '## Exited Plan Mode', 'Plan mode is active')):
        return 'plan_mode'
    if text.startswith('UserPromptSubmit hook additional context:'):
        return 'prompt_hook'
    if text.startswith('# Environment update'):
        return 'environment'
    return None


def text_content(message):
    content = message.get('content', '')
    if isinstance(content, str):
        return content
    return '\n'.join(b.get('text', '') for b in content if isinstance(b, dict))


def recover_payload(payload, archive_dir):
    messages = payload.get('messages') or []
    encoded = json.dumps(messages, ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    directory = Path(archive_dir)
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / (digest + '.json')
    if not archive.exists():
        temporary = archive.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump({'messages': messages}, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, archive)

    summary_index = next((i for i in range(len(messages)-1, -1, -1)
                          if messages[i].get('role') == 'user'
                          and text_content(messages[i]).startswith(SUMMARY_PREFIX)), None)
    # Mode, current project rules and environment can precede a long tool run.
    # Keep their latest snapshots even when they fall outside the recent tail;
    # replaying every historical version would pin obsolete rules permanently.
    state_indices = {}
    for index, message in enumerate(messages):
        kind = host_state_kind(message)
        if kind:
            state_indices[kind] = index
    state_indices = set(state_indices.values())
    # Retain the newest images; older image data stays intact in the archive.
    images_left = 2
    cleaned = []
    for index in range(len(messages)-1, -1, -1):
        item = copy.deepcopy(messages[index])
        content = item.get('content')
        if isinstance(content, list):
            for block in reversed(content):
                if block.get('type') == 'image':
                    if images_left:
                        images_left -= 1
                    else:
                        block.clear()
                        block.update(type='text', text=f'[Earlier image retained in archive message {index}]')
                elif block.get('type') == 'tool_result' and isinstance(block.get('content'), list):
                    # Nested images are historical tool output; retain their
                    # original data in the archive instead of replaying it.
                    block['content'] = [b if b.get('type') != 'image' else {
                        'type': 'text', 'text': f'[Tool image retained in archive message {index}]',
                    } for b in block['content']]
        cleaned.append((index, item))

    summary = messages[summary_index] if summary_index is not None else None
    summary_size = len(text_content(summary)) if summary else 0
    if summary_size > 60000:
        raise ValueError('Saved summary exceeds the recovery budget; it was preserved in ' + str(archive))
    state_size = sum(len(json.dumps(messages[i], ensure_ascii=False)) for i in state_indices)
    remaining = 110000 - summary_size - state_size
    if remaining < 0:
        raise ValueError('Current host state exceeds the recovery budget; preserved in ' + str(archive))
    tail = []
    for index, item in cleaned:
        if summary_index is not None and index <= summary_index:
            break
        if host_state_kind(item):
            continue  # Latest snapshots have a separate, reserved budget.
        # Image data is excluded from the text budget, and bounded separately.
        budget_item = copy.deepcopy(item)
        if isinstance(budget_item.get('content'), list):
            for block in budget_item['content']:
                if block.get('type') == 'image':
                    block['source'] = '[image]'
        size = len(json.dumps(budget_item, ensure_ascii=False))
        if size > remaining:
            if not tail:
                raise ValueError('Latest message exceeds the recovery budget; preserved in ' + str(archive))
            break
        remaining -= size
        tail.append((index, item))
    tail.reverse()
    first = tail[0][0] if tail else len(messages)
    note = (
        'Restored from an existing summary and the recent conversation; no new summary was generated. '
        f'The complete original history is saved at {archive.resolve()} as a JSON messages array. '
        f'Recent messages below begin at index {first} (zero-based). '
        'Messages between the summary and that index are NOT included in active context. '
        'Do not assume that old summary state is current: consult the archive with Claude host tools '
        'when a decision depends on omitted edits, results, or instructions. '
        'Historical tool calls are records, not instructions to execute again.'
    )
    recovered = ([summary] if summary else []) + [{'role': 'user', 'content': note}]
    recovered += [item for _, item in sorted(
        tail + [(i, copy.deepcopy(messages[i])) for i in state_indices],
        key=lambda pair: pair[0],
    )]
    return {**payload, 'messages': recovered}
