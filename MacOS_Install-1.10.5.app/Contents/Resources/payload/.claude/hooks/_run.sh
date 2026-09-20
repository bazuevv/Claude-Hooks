#!/usr/bin/env bash
# Cross-platform Python hook dispatcher.
#
# Usage: bash .claude/hooks/_run.sh <hook-script.py> [args...]
#
# Зачем нужен:
#   - Хукам нужен Python 3.11+ (tomllib); системный Python macOS старше.
#   - На Linux канонический Python — `python3`.
#   - На Windows `python3.exe` обычно битый Microsoft Store-стаб
#     (exit 49 без сообщения), а реальный исполняемый файл называется
#     `python.exe` или вызывается через лаунчер `py -3`.
#   - Один settings.json должен работать на Windows, Linux и macOS.
#
# Скрипт перебирает python3, версионные имена, python и py -3,
# выбирая первый работающий Python 3.11+. Проверка версии также
# отбрасывает Windows Store-стабы.
#
# Пути:
#   - Хук ищем относительно $0, независимо от текущего каталога.

# НЕ ставим `set -e`: ниже используем return-коды для перебора кандидатов,
# и нам нужно дойти до конца списка даже если первые два упали.

SCRIPT_NAME="${1:-}"
if [ -z "$SCRIPT_NAME" ]; then
    echo "Usage: $0 <hook-script.py> [args...]" >&2
    exit 1
fi
shift

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$HOOK_DIR/python_probe.sh"
HOOK_PATH="$HOOK_DIR/$SCRIPT_NAME"

# install.sh/install.ps1 record machine-local paths for GUI-launched VS Code. Read them as
# data, never source/eval them; Windows paths need conversion inside Git Bash.
INSTALL_STATE="$HOOK_DIR/../install-state"
host_path() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -u "$1"
    else
        printf '%s\n' "$1"
    fi
}
if [ -f "$INSTALL_STATE/bin-paths.txt" ]; then
    while IFS= read -r directory; do
        directory=$(host_path "$directory")
        if [ -d "$directory" ]; then
            export PATH="$directory:$PATH"
        fi
    done < "$INSTALL_STATE/bin-paths.txt"
fi

# Принудительно ставим UTF-8 для всех Python I/O. На Windows Python по
# умолчанию использует системную ANSI codepage (cp1251 для русской
# локали), из-за чего json.load(sys.stdin) падает на кириллице, а
# print() с UTF-8 портит вывод.
# PYTHONUTF8=1 (Python 3.7+) — глобальный UTF-8 mode, покрывает stdin,
# stdout, stderr, fs paths.
# PYTHONIOENCODING — fallback на случай если UTF-8 mode выключен.
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

if [ ! -f "$HOOK_PATH" ]; then
    echo "Hook script not found: $HOOK_PATH" >&2
    exit 1
fi

# Пробуем интерпретатор по имени; если он есть и не битый — exec.
# `exec` заменяет процесс bash на python, stdin/stdout/stderr и код
# возврата пробрасываются прозрачно.
try_python() {
    local py="$1"
    shift  # убрать имя интерпретатора, остаются хук-аргументы
    if ! safe_python_candidate "$py"; then
        return 1
    fi
    # Битый Windows Store-стаб выходит с кодом 49 без вывода версии.
    if ! "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' </dev/null >/dev/null 2>&1; then
        return 1
    fi
    exec "$py" "$HOOK_PATH" "$@"
}

# Явный путь полезен для нестандартной установки и GUI с урезанным PATH.
if [ -n "${CLAUDE_HOOK_PYTHON:-}" ]; then
    try_python "$(host_path "$CLAUDE_HOOK_PYTHON")" "$@"
    echo "CLAUDE_HOOK_PYTHON must point to a working Python 3.11+: $CLAUDE_HOOK_PYTHON" >&2
    exit 1
fi

if [ -f "$INSTALL_STATE/python-path.txt" ]; then
    IFS= read -r installed_python < "$INSTALL_STATE/python-path.txt"
    try_python "$(host_path "$installed_python")" "$@"
fi

# Managed macOS runtime is also discoverable before install-state is saved.
for managed_python in "$HOME/Library/Application Support/ClaudeHooks/tools/python/"*/bin/python3.13; do
    [ ! -x "$managed_python" ] || try_python "$managed_python" "$@"
done
try_python python3 "$@"
for version in 3.14 3.13 3.12 3.11; do
    try_python "python$version" "$@"
done
try_python python "$@"
for version in 3.14 3.13 3.12 3.11; do
    try_python "$HOME/.local/bin/python$version" "$@"
done

# `py` — Python launcher на Windows; флаг `-3` выбирает Python 3.x.
if command -v py >/dev/null 2>&1; then
    if py -3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' </dev/null >/dev/null 2>&1; then
        exec py -3 "$HOOK_PATH" "$@"
    fi
fi

# VS Code, запущенный из Finder, может не видеть Homebrew/python.org.
# Не меняем PATH и не читаем пользовательские shell startup-файлы.
if [ "$(uname -s)" = "Darwin" ]; then
    for directory in /opt/homebrew/bin /usr/local/bin /opt/local/bin \
        /opt/homebrew/opt/python@3.*/bin /usr/local/opt/python@3.*/bin \
        /Library/Frameworks/Python.framework/Versions/3.*/bin; do
        try_python "$directory/python3" "$@"
        for version in 3.14 3.13 3.12 3.11; do
            try_python "$directory/python$version" "$@"
        done
    done
fi

echo "Python 3.11+ is required for Claude hooks (including tomllib). Install it and restart VS Code, or set CLAUDE_HOOK_PYTHON to its executable path." >&2
exit 1
