#!/usr/bin/env bash
set -e
cd -- "$(dirname -- "$0")"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/phonecam"
export PATH="$DATA/bin:$PATH"

# Apply an update that was downloaded in the background last time (instant, offline-safe).
if [ -d .git ] && command -v git >/dev/null; then
    git merge --ff-only --quiet '@{u}' >/dev/null 2>&1 || true
fi

open_terminal() {
    for terminal in konsole gnome-terminal kgx xfce4-terminal kitty alacritty foot xterm x-terminal-emulator; do
        command -v "$terminal" >/dev/null || continue
        case $terminal in
            gnome-terminal|kgx) exec "$terminal" -- "$@" ;;
            kitty|foot) exec "$terminal" "$@" ;;
            *) exec "$terminal" -e "$@" ;;
        esac
    done
    notify-send PhoneCam "Τρέξε $PWD/install.sh σε τερματικό." 2>/dev/null || true
    exit 1
}

if ! ./install.sh --check --quiet; then
    open_terminal bash -c './install.sh; echo; read -r -p "Enter για κλείσιμο… " _'
fi

PYTHON=python3
[ -x "$DATA/venv/bin/python" ] && PYTHON="$DATA/venv/bin/python"
exec "$PYTHON" -m phonecam "$@"
