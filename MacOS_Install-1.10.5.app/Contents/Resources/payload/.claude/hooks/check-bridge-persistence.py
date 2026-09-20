"""Live check of durable history and catalog changes; never executes host tools."""
import json
import uuid
from pathlib import Path

from codex_anthropic_bridge import CodexTextBackend, collect_message


def main():
    run = uuid.uuid4().hex
    runtime = Path(__file__).resolve().parent.parent / 'hooks-runtime' / ('persistence-check-' + run)
    runtime.mkdir()
    settings = json.loads((Path.home() / '.claude/settings_openai.json').read_text(encoding='utf-8'))
    payload = {'model': settings['env']['ANTHROPIC_MODEL'],
               'metadata': {'session_id': 'probe-' + run},
               'tools': [{'name': 'Probe', 'input_schema': {'type': 'object'}}],
               'messages': [{'role': 'user', 'content': 'Remember the marker blue-pine-427. Reply OK only. Do not use tools.'}]}
    state = str(runtime / 'usage.json')
    with_backend = CodexTextBackend(usage_state_file=state).start()
    try:
        first = collect_message(with_backend.begin(payload))
        assert first['stop_reason'] == 'end_turn'
        tid = with_backend._sessions['probe-' + run].thread_id
    finally:
        with_backend.close()
    backend = CodexTextBackend(usage_state_file=state).start()
    try:
        payload['tools'].append({'name': 'ListAgents', 'input_schema': {'type': 'object'}})
        payload['messages'].append({'role': 'user', 'content': 'What exact marker did I ask you to remember? Reply with the marker only. Do not use tools.'})
        result = collect_message(backend.begin(payload))
        assert backend._sessions['probe-' + run].thread_id == tid
        assert 'blue-pine-427' in json.dumps(result['content']), result['content']
        print('PASS: process restart + added tool preserved the same Codex thread and remembered context')
    finally:
        backend.close()


if __name__ == '__main__':
    main()
