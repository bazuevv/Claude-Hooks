#!/usr/bin/env bash
# Bash 3.2 compatible. Windows without Bash: run install.ps1 instead.
# Bash 3.2 treats even declared empty arrays as unset under `set -u`.
set -e
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$INSTALL_DIR/hooks/python_probe.sh"
case "${1:-}" in
    --help|-h)
        echo 'Usage: bash .claude/install.sh [--check|--report|--global]'
        echo 'Install missing hook dependencies; --check makes no changes.'
        exit 0 ;;
    ''|--check|--report|--global) ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { echo 'Too many arguments' >&2; exit 2; }
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        args=()
        [ "${1:-}" != --check ] || args=(-Check)
        [ "${1:-}" != --report ] || args=(-Report)
        [ "${1:-}" != --global ] || args=(-Global)
        exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File \
            "$(cygpath -w "$INSTALL_DIR/install.ps1")" "${args[@]}" ;;
    Darwin|Linux) ;;
    *) echo 'Supported systems: Windows, Linux, macOS' >&2; exit 1 ;;
esac

# Reuse exactly the same interpreter selection as the hooks.
if hook_python=$(bash "$INSTALL_DIR/hooks/_run.sh" install_dependencies.py --probe-python 2>/dev/null); then
    exec "$hook_python" -B "$INSTALL_DIR/hooks/install_dependencies.py" "$@"
fi
if [ "${1:-}" = --report ]; then
    # Reports run on the system Python 3.9 as well, without installing Python.
    for candidate in python3 python /usr/bin/python3; do
        safe_python_candidate "$candidate" || continue
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' </dev/null >/dev/null 2>&1; then
            exec "$candidate" -B "$INSTALL_DIR/hooks/install_dependencies.py" --report
        fi
    done
    printf '%s\n' '@claude-installer {"id":"python","state":"missing"}'
    # These probes do not need Python. Missing Python must not make built-in
    # macOS tools look absent on a clean machine.
    printf '%s\n' '@claude-installer {"id":"bash","state":"ready"}'
    audio_state=missing
    if [ "$(uname -s)" = Darwin ] && [ -x /usr/bin/afplay ]; then
        audio_state=ready
    elif command -v ffplay >/dev/null 2>&1 && ffplay -version </dev/null >/dev/null 2>&1; then
        audio_state=ready
    fi
    printf '@claude-installer {"id":"audio","state":"%s"}\n' "$audio_state"
    for dependency in node code claude codex; do
        printf '@claude-installer {"id":"%s","state":"unknown"}\n' "$dependency"
    done
    exit 1
fi
if [ "${1:-}" = --check ]; then
    echo '[MISSING] Python 3.11+ with tomllib, ssl, sqlite3 and the hook standard-library modules.' >&2
    echo 'Run bash .claude/install.sh to install it and check the remaining dependencies.' >&2
    exit 1
fi
if [ -n "${CLAUDE_HOOK_PYTHON:-}" ]; then
    echo 'CLAUDE_HOOK_PYTHON is invalid or incomplete. Unset it and rerun install.' >&2
    exit 1
fi

# macOS uses a verified relocatable build, without calling install_name_tool.
if [ "$(uname -s)" = Darwin ]; then
    bash "$INSTALL_DIR/hooks/install_python_macos.sh"
    exec "$HOME/Library/Application Support/ClaudeHooks/tools/python/3.13.15/bin/python3.13" -B "$INSTALL_DIR/hooks/install_dependencies.py" "$@"
fi

# uv installs a separate user Python, leaving the OS interpreter intact.
if [ "${CLAUDE_INSTALL_EVENTS:-}" = 1 ]; then
    printf '%s\n' '@claude-installer {"id":"python","state":"installing"}'
fi
uv_bin=$(command -v uv || true)
if [ -z "$uv_bin" ] && [ -x "$HOME/.local/bin/uv" ]; then
    uv_bin="$HOME/.local/bin/uv"
fi
if [ -z "$uv_bin" ]; then
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        elevate=()
        [ "$EUID" -eq 0 ] || elevate=(sudo)
        if command -v apt-get >/dev/null 2>&1; then
            "${elevate[@]}" apt-get update
            "${elevate[@]}" apt-get install -y curl ca-certificates
        elif command -v dnf >/dev/null 2>&1; then
            "${elevate[@]}" dnf install -y curl ca-certificates
        elif command -v pacman >/dev/null 2>&1; then
            "${elevate[@]}" pacman -S --needed --noconfirm curl ca-certificates
        elif command -v zypper >/dev/null 2>&1; then
            "${elevate[@]}" zypper --non-interactive install curl ca-certificates
        else
            echo 'No supported package manager to install curl; install curl and rerun.' >&2
            exit 1
        fi
    fi
    download=$(mktemp)
    trap 'rm -f "$download"' EXIT
    echo 'Installing uv from https://astral.sh/uv/install.sh'
    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --connect-timeout 30 --max-time 600 --proto '=https' --tlsv1.2 https://astral.sh/uv/install.sh -o "$download"
    elif command -v wget >/dev/null 2>&1; then
        wget --https-only https://astral.sh/uv/install.sh -O "$download"
    else
        echo 'curl or wget is required to download Python.' >&2
        exit 1
    fi
    UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$download"
    uv_bin="$HOME/.local/bin/uv"
    rm -f "$download"
    trap - EXIT
fi
"$uv_bin" python install 3.13
hook_python=$("$uv_bin" python find --managed-python 3.13)
if ! "$hook_python" -B "$INSTALL_DIR/hooks/install_dependencies.py" --probe-python >/dev/null; then
    "$uv_bin" python install --reinstall 3.13
    hook_python=$("$uv_bin" python find --managed-python 3.13)
fi
exec "$hook_python" -B "$INSTALL_DIR/hooks/install_dependencies.py" "$@"
