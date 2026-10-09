"""One-click system report for remote troubleshooting (no personal data, no stream URLs)."""
import os
from pathlib import Path
import platform
import subprocess
from . import __version__
from .driver import tool


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ''


def distribution():
    for line in read('/etc/os-release').splitlines():
        if line.startswith('PRETTY_NAME='):
            return line.split('=', 1)[1].strip('"')
    return 'άγνωστη'


def loopback_version():
    version = read('/sys/module/v4l2loopback/version')
    if version:
        return version + ' (φορτωμένος)'
    try:
        result = subprocess.run([tool('modinfo'), '-F', 'version', 'v4l2loopback'], capture_output=True, text=True, timeout=5)
        return (result.stdout.strip() + ' (εγκατεστημένος, όχι φορτωμένος)') if result.stdout.strip() else 'δεν υπάρχει'
    except (OSError, subprocess.TimeoutExpired):
        return 'δεν υπάρχει'


def holders(device):
    """Programs that have the virtual camera open right now (e.g. Discord)."""
    names = set()
    try:
        target = os.path.realpath(device)
    except OSError:
        return []
    for process in Path('/proc').glob('[0-9]*'):
        try:
            for handle in (process / 'fd').iterdir():
                if os.path.realpath(handle) == target:
                    names.add(read(process / 'comm'))
                    break
        except OSError:
            continue
    return sorted(name for name in names if name)


def cameras():
    lines = []
    for folder in sorted(Path('/sys/class/video4linux').glob('video*')):
        lines.append(f'  /dev/{folder.name}: {read(folder / "name")}')
    return lines or ['  καμία']


def report(window):
    capture = window.capture
    device = window.device_path
    cpu = read('/proc/cpuinfo')
    lines = [
        f'PhoneCam {__version__}' + (' (AppImage)' if os.environ.get('APPIMAGE') else ' (από φάκελο)'),
        f'Διανομή: {distribution()}',
        f'Kernel: {platform.release()}',
        f'Επιφάνεια: {os.environ.get("XDG_CURRENT_DESKTOP", "?")} · {os.environ.get("XDG_SESSION_TYPE", "?")}',
        f'CPU x86-64-v2: {"ναι" if " sse4_2" in cpu and " popcnt" in cpu else "όχι"}',
        f'v4l2loopback: {loopback_version()}',
        f'Secure Boot: {"ενεργό" if read("/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-e0e8fe0e8b8c")[-1:] == chr(1) else "όχι/άγνωστο"}',
        'Κάμερες:', *cameras(),
        f'Virtual camera: {device or "—"} «{window.device_name or "—"}»',
        f'Ανοιχτή από: {", ".join(holders(device)) or "κανένα πρόγραμμα"}' if device else 'Ανοιχτή από: —',
        f'Πηγή: {capture.source} · {capture.size} · {capture.fps} FPS · έξοδος YUYV',
        f'Σύνδεση: {"ενεργή" if window.engine.want_capture else "όχι"} · μετρημένα: {window.badge.text()}',
        f'Φόντο: {"ναι" if capture.background else "κανένα"}',
        'Τελευταία μηνύματα:',
    ]
    logs = window.engine.logs
    if capture.url:
        logs = logs.replace(capture.url, '[stream]')
    lines += ['  ' + line for line in logs.strip().splitlines()[-8:]] or ['  —']
    return '\n'.join(lines)
