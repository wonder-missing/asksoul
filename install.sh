#!/bin/sh
# AskSoul installer (POSIX sh, macOS and Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/wonder-missing/asksoul/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --bot
#
# Environment:
#   ASKSOUL_REF       git ref to download from (default: main)
#   ASKSOUL_BIN_DIR   install directory (overrides PREFIX and the defaults)
#   PREFIX            prefix for the default ~/.local-style install
#   TMPDIR            temporary directory

set -eu

REPO="wonder-missing/asksoul"
REF="${ASKSOUL_REF:-main}"
BASE="https://raw.githubusercontent.com/${REPO}/${REF}"

usage() {
    cat <<'EOF'
usage: install.sh [--bot]

  (no flag)   install the asksoul CLI
  --bot       install the asksoul-talk bot helper

Environment: ASKSOUL_REF, ASKSOUL_BIN_DIR, PREFIX.
EOF
}

BOT=0
for arg in "$@"; do
    case "$arg" in
        --bot) BOT=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if ! command -v python3 >/dev/null 2>&1; then
    echo "install.sh: python3 not found; install Python 3.9 or newer" >&2
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "install.sh: Python 3.9 or newer is required (found $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])'))" >&2
    exit 1
fi

if [ "$BOT" -eq 1 ]; then
    NAME="asksoul-talk"
    SRC="bot/asksoul-talk"
    if [ -n "${ASKSOUL_BIN_DIR:-}" ]; then
        DIR="$ASKSOUL_BIN_DIR"
    elif [ -d "$HOME/bin" ]; then
        DIR="$HOME/bin"
    else
        DIR="${PREFIX:-$HOME/.local}/bin"
    fi
else
    NAME="asksoul"
    SRC="bin/asksoul"
    DIR="${ASKSOUL_BIN_DIR:-${PREFIX:-$HOME/.local}/bin}"
fi

URL="${BASE}/${SRC}"
TMP="${TMPDIR:-/tmp}/asksoul-install.$$"
trap 'rm -f "$TMP"' EXIT HUP INT TERM

echo "installing ${NAME} from ${URL}"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$TMP"
elif command -v wget >/dev/null 2>&1; then
    wget -qO "$TMP" "$URL"
else
    echo "install.sh: curl or wget is required" >&2
    exit 1
fi

mkdir -p "$DIR"
if command -v install >/dev/null 2>&1; then
    install -m 755 "$TMP" "$DIR/$NAME"
else
    cp "$TMP" "$DIR/$NAME"
    chmod 755 "$DIR/$NAME"
fi

echo "installed $DIR/$NAME"
case ":${PATH}:" in
    *":${DIR}:"*) ;;
    *)
        echo "install.sh: note: ${DIR} is not in PATH; add it, for example:" >&2
        echo "  export PATH=\"${DIR}:\$PATH\"" >&2
        ;;
esac
