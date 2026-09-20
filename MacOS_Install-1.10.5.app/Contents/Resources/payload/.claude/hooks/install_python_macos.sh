#!/bin/bash
# Ready-to-run python-build-standalone; no uv dylib fixup / Apple developer shims.
# Pins and SHA256 come from astral-sh/uv crates/uv-python/download-metadata.json.
set -e
set -o pipefail
case "$(uname -m)" in
    arm64) arch=aarch64; checksum=d3904bd6a072246e07aa0bdadee9a14e80521e42a943c0848059feb16a2816dc ;;
    x86_64) arch=x86_64; checksum=f712a9143c8a5d248438ec7921a0b48d548bca4f1337d33c690d28c2d0504137 ;;
    *) echo 'Unsupported macOS architecture' >&2; exit 1 ;;
esac
root="$HOME/Library/Application Support/ClaudeHooks/tools/python"
mkdir -p "$root"
work=$(mktemp -d "$root/.download-XXXXXX")
trap 'rm -rf "$work"' EXIT
emit_stage() {
    [ "${CLAUDE_INSTALL_EVENTS:-}" != 1 ] || printf '@claude-installer {"id":"python","state":"installing","stage":"%s"}\n' "$1"
}
emit_stage downloading
url="https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.13.15%2B20260901-$arch-apple-darwin-install_only_stripped.tar.gz"
/usr/bin/curl --fail --location --progress-bar --connect-timeout 30 --max-time 1800 \
    --proto '=https' --proto-redir '=https' --tlsv1.2 "$url" -o "$work/python.tar.gz" 2>&1 | \
    /usr/bin/awk 'BEGIN { RS="\r"; last=-1 } {
        if (match($0, /[0-9]+\.[0-9]+%/)) {
            value=int(substr($0, RSTART, RLENGTH));
            if (value != last && ENVIRON["CLAUDE_INSTALL_EVENTS"] == "1") {
                printf "@claude-installer {\"id\":\"python\",\"state\":\"installing\",\"stage\":\"downloading\",\"percent\":%d}\n", value;
                fflush(); last=value;
            }
        } else if (index($0, "curl:")) { print; fflush(); }
    }'
emit_stage verifying
actual=$(/usr/bin/shasum -a 256 "$work/python.tar.gz")
[ "${actual%% *}" = "$checksum" ] || { echo 'Python checksum mismatch' >&2; exit 1; }
emit_stage extracting
/usr/bin/tar -xzf "$work/python.tar.gz" -C "$work"
emit_stage installing
# Each revision has its own directory. Never replace a pre-existing interpreter.
target="$root/3.13.15"
if [ -e "$target" ]; then
    echo "Existing Python installation needs repair: $target" >&2
    exit 1
fi
mv "$work/python" "$target"
emit_stage verifying
"$target/bin/python3.13" -B "$(dirname "$0")/install_dependencies.py" --probe-python
