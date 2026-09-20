"""Declare the verified bridge context window in the OpenAI profile."""
import json
import shutil
from datetime import datetime
from pathlib import Path
import account_switcher


def main():
    runtime = Path(__file__).resolve().parent.parent / 'hooks-runtime'
    usage = json.loads((runtime / 'codex-bridge-usage.json').read_text(encoding='utf-8'))
    window = usage.get('model_context_window')
    model = usage.get('model')
    if not isinstance(window, int) or window < 100000 or not model:
        raise SystemExit('No verified model context window in bridge telemetry')
    home = Path.home() / '.claude'
    changes = []
    for path in (home / 'settings_openai.json', home / 'settings.json'):
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        env = data.get('env', {})
        if env.get('ANTHROPIC_BASE_URL') != 'http://127.0.0.1:18925':
            continue
        if env.get('ANTHROPIC_MODEL') != model:
            raise SystemExit('Profile model differs from verified bridge model')
        for key in ('CLAUDE_CODE_MAX_CONTEXT_TOKENS', 'CLAUDE_CODE_AUTO_COMPACT_WINDOW'):
            env[key] = str(window)
        changes.append((path, data))
    backup = home / 'backups' / ('context-window-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True, exist_ok=False)
    for path, data in changes:
        shutil.copy2(path, backup / path.name)
        account_switcher._write_json_atomic(str(path), data)
        saved = json.loads(path.read_text(encoding='utf-8'))
        assert saved['env']['CLAUDE_CODE_MAX_CONTEXT_TOKENS'] == str(window)
        print(json.dumps({'file': str(path), 'model': model, 'window': window}))


if __name__ == '__main__':
    main()
