#!/usr/bin/env bash
# PhoneCam setup: checks every dependency and installs whatever is missing.
#   ./install.sh            check, install missing pieces (asks for the admin password once)
#   ./install.sh --check    only report; exit 1 if something is missing
#   ./install.sh --yes      do not ask before installing
set -u
cd -- "$(dirname -- "$0")"
ROOT=$PWD
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/phonecam"
BIN="$DATA/bin"
VENV="$DATA/venv"
export PATH="$BIN:$PATH"
CHECK=0 YES=0 QUIET=0
for argument in "$@"; do
    case $argument in
        --check) CHECK=1 ;;
        --yes|-y) YES=1 ;;
        --quiet) QUIET=1 ;;
    esac
done

say() { [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }
good() { say "  ✓ $*"; }
bad() { say "  ✗ $*"; }

# --- what is there -----------------------------------------------------------

python_for_app() {
    if [ -x "$VENV/bin/python" ]; then echo "$VENV/bin/python"; return; fi
    for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
        if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            command -v "$candidate"; return
        fi
    done
}

python_ok() {
    local python
    python=$(python_for_app)
    [ -n "$python" ] && "$python" - <<'EOF' 2>/dev/null
import PySide6, numpy, cv2
assert hasattr(cv2.dnn, 'readNetFromTFLite')  # OpenCV 4.8+
EOF
}

version_at_least() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

scrcpy_ok() {
    command -v scrcpy >/dev/null || return 1
    version_at_least "$(scrcpy --version 2>/dev/null | head -1 | awk '{print $2}')" 2.2
}

ffmpeg_ok() { command -v ffmpeg >/dev/null && ffmpeg -hide_banner -filters 2>/dev/null | grep -qw zmq; }

loopback_ok() {
    [ -d /sys/module/v4l2loopback ] && return 0
    modinfo v4l2loopback >/dev/null 2>&1 || /sbin/modinfo v4l2loopback >/dev/null 2>&1
}

missing=()
report() {
    missing=()
    say "PhoneCam · έλεγχος εξαρτήσεων"
    if python_ok; then good "Python + PySide6 + OpenCV + numpy ($(python_for_app))"; else bad "Python 3.11+ με PySide6, OpenCV 4.8+, numpy"; missing+=(python); fi
    if ffmpeg_ok; then good "FFmpeg (με zmq)"; else bad "FFmpeg με zmq filter"; missing+=(ffmpeg); fi
    if command -v adb >/dev/null; then good "adb"; else bad "adb (android-tools)"; missing+=(adb); fi
    if scrcpy_ok; then good "scrcpy $(scrcpy --version 2>/dev/null | head -1 | awk '{print $2}')"; else bad "scrcpy 2.2+ (κάμερα κινητού)"; missing+=(scrcpy); fi
    if loopback_ok; then good "v4l2loopback (virtual camera)"; else bad "v4l2loopback driver"; missing+=(loopback); fi
    if command -v git >/dev/null; then good "git (αυτόματες ενημερώσεις)"; else bad "git (για αυτόματες ενημερώσεις)"; missing+=(git); fi
    if command -v pkexec >/dev/null; then good "pkexec"; else bad "pkexec (polkit)"; missing+=(polkit); fi
    [ ${#missing[@]} -eq 0 ]
}

# --- installing ----------------------------------------------------------------

as_root() {
    if [ "$(id -u)" = 0 ]; then "$@"
    elif command -v sudo >/dev/null && [ -t 0 ]; then sudo "$@"
    else pkexec "$@"
    fi
}

install_scrcpy_release() {
    # Distribution packages are often too old for camera capture; use the official static build.
    local arch url folder
    arch=$(uname -m)
    [ "$arch" = x86_64 ] || { bad "Δεν υπάρχει έτοιμο scrcpy για $arch — εγκατάστησέ το από τη διανομή σου (2.2+)."; return 1; }
    url=$(curl -fsSL https://api.github.com/repos/Genymobile/scrcpy/releases/latest | grep -o 'https://[^"]*scrcpy-linux-x86_64[^"]*\.tar\.gz' | head -1)
    [ -n "$url" ] || { bad "Δεν βρέθηκε η τελευταία έκδοση scrcpy."; return 1; }
    say "  → λήψη $url"
    folder="$DATA/scrcpy"
    rm -rf "$folder" && mkdir -p "$folder" "$BIN"
    curl -fsSL "$url" | tar -xz -C "$folder" --strip-components=1 || return 1
    ln -sf "$folder/scrcpy" "$BIN/scrcpy"
    [ -x "$folder/adb" ] && ! command -v adb >/dev/null && ln -sf "$folder/adb" "$BIN/adb"
    good "scrcpy στο $folder"
}

install_python_env() {
    local python
    python=$(python_for_app)
    [ -n "$python" ] || { bad "Χρειάζεται Python 3.11 ή νεότερη."; return 1; }
    say "  → ιδιωτικό περιβάλλον Python στο $VENV"
    rm -rf "$VENV"
    "$python" -m venv "$VENV" || return 1
    "$VENV/bin/python" -m pip install --quiet --upgrade pip &&
        "$VENV/bin/python" -m pip install --quiet 'PySide6>=6.6,<7' 'numpy>=1.26' 'opencv-python-headless>=4.8'
}

kernel_headers_arch() {
    local kernel
    kernel=$(pacman -Qqo "/usr/lib/modules/$(uname -r)" 2>/dev/null | head -1)
    [ -n "$kernel" ] && echo "$kernel-headers"
}

install_missing() {
    local manager='' packages=() driver=()
    if command -v pacman >/dev/null; then manager=pacman
    elif command -v apt-get >/dev/null; then manager=apt
    elif command -v dnf >/dev/null; then manager=dnf
    fi
    for item in "${missing[@]}"; do
        case $manager:$item in
            pacman:python) packages+=(python python-pyside6 python-numpy python-opencv) ;;
            pacman:ffmpeg) packages+=(ffmpeg) ;;
            pacman:adb) packages+=(android-tools) ;;
            pacman:scrcpy) packages+=(scrcpy) ;;
            pacman:loopback) driver+=(v4l2loopback-dkms v4l2loopback-utils $(kernel_headers_arch)) ;;
            pacman:git) packages+=(git) ;;
            pacman:polkit) packages+=(polkit) ;;
            apt:python) packages+=(python3 python3-venv python3-pip) ;;
            apt:ffmpeg) packages+=(ffmpeg) ;;
            apt:adb) packages+=(adb) ;;
            apt:loopback) driver+=(v4l2loopback-dkms v4l2loopback-utils "linux-headers-$(uname -r)") ;;
            apt:git) packages+=(git) ;;
            apt:polkit) packages+=(pkexec) ;;
            dnf:python) packages+=(python3 python3-pip) ;;
            dnf:ffmpeg) packages+=(ffmpeg) ;;
            dnf:adb) packages+=(android-tools) ;;
            dnf:loopback) driver+=(v4l2loopback akmod-v4l2loopback kernel-devel) ;;
            dnf:git) packages+=(git) ;;
            dnf:polkit) packages+=(polkit) ;;
        esac
    done
    if [ -n "$manager" ] && [ $((${#packages[@]} + ${#driver[@]})) -gt 0 ]; then
        say ""
        say "Θα εγκατασταθούν: ${packages[*]} ${driver[*]}"
        if [ "$YES" != 1 ] && [ -t 0 ]; then
            read -r -p "Συνέχεια; [Y/n] " answer
            case $answer in [nNοΟ]*) exit 1 ;; esac
        fi
        if [ "$manager" = dnf ] && [[ " ${missing[*]} " =~ " ffmpeg " || " ${missing[*]} " =~ " loopback " ]] && ! dnf repolist 2>/dev/null | grep -qi rpmfusion; then
            say "Το Fedora χρειάζεται RPM Fusion για FFmpeg και v4l2loopback: https://rpmfusion.org/Configuration"
        fi
        [ "$manager" = apt ] && as_root apt-get update -qq
        for group in packages driver; do
            local -n list=$group
            [ ${#list[@]} -gt 0 ] || continue
            case $manager in
                pacman) as_root pacman -S --needed --noconfirm "${list[@]}" ;;
                apt) as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${list[@]}" ;;
                dnf) as_root dnf install -y "${list[@]}" ;;
            esac || bad "Αποτυχία εγκατάστασης: ${list[*]}"
        done
    elif [ -z "$manager" ]; then
        say "Άγνωστη διανομή: εγκατάστησε χειροκίνητα τα παραπάνω που λείπουν."
    fi
    # Pieces the package manager may not cover (or covers with versions that are too old).
    scrcpy_ok || install_scrcpy_release
    python_ok || install_python_env
}

# --- main ----------------------------------------------------------------------

if report; then
    [ "$CHECK" = 1 ] && exit 0
else
    [ "$CHECK" = 1 ] && exit 1
    install_missing
    say ""
    report || { say ""; say "Κάποια εξάρτηση λείπει ακόμα — δες τα ✗ παραπάνω."; exit 1; }
fi
"$(python_for_app)" "$ROOT/install_launchers.py" >/dev/null && good "Εικονίδια εφαρμογής"
say ""
say "Έτοιμο. Άνοιξε το PhoneCam από το μενού ή την επιφάνεια εργασίας."
