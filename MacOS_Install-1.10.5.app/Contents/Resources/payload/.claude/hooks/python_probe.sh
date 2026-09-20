#!/usr/bin/env bash
# Sourced by the Bash 3.2 dispatcher and installer. Never execute Apple's
# developer-tool shims: even a --version probe can open the CLT installer.
safe_python_candidate() {
    local candidate resolved link directory count
    candidate=$(command -v "$1") || return 1
    [ "$(uname -s)" = Darwin ] || return 0
    resolved="$candidate"
    count=0
    while [ -L "$resolved" ]; do
        count=$((count + 1))
        [ "$count" -le 40 ] || return 1
        link=$(readlink "$resolved") || return 1
        case "$link" in
            /*) resolved="$link" ;;
            *) resolved="$(dirname "$resolved")/$link" ;;
        esac
    done
    directory=$(cd "$(dirname "$resolved")" 2>/dev/null && pwd -P) || return 1
    case "$directory/$(basename "$resolved")" in
        /usr/bin/python|/usr/bin/python[0-9]*) return 1 ;;
    esac
    return 0
}
