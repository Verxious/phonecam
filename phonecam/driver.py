"""The v4l2loopback kernel driver: the one part no AppImage can carry."""
from pathlib import Path
import platform
import shlex
import shutil
import subprocess


def tool(name):
    # pkexec needs an absolute path; distributions keep these in bin or sbin.
    for folder in ('/usr/bin', '/usr/sbin', '/sbin', '/bin'):
        if Path(folder, name).exists():
            return f'{folder}/{name}'
    return shutil.which(name) or name


def available():
    if Path('/sys/module/v4l2loopback').exists():
        return True
    try:
        return subprocess.run([tool('modinfo'), 'v4l2loopback'], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def install_script():
    """Shell commands that install the driver on this distribution, or '' if unknown."""
    kernel = platform.release()
    if shutil.which('pacman'):
        try:
            owner = subprocess.run(['pacman', '-Qqo', f'/usr/lib/modules/{kernel}'], capture_output=True, text=True, timeout=10).stdout.split()
        except (OSError, subprocess.TimeoutExpired):
            owner = []
        headers = [owner[0] + '-headers'] if owner else []
        return 'pacman -S --needed --noconfirm ' + shlex.join(['v4l2loopback-dkms', 'v4l2loopback-utils', *headers])
    if shutil.which('apt-get'):
        return 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y ' + shlex.join(['v4l2loopback-dkms', 'v4l2loopback-utils', f'linux-headers-{kernel}'])
    if shutil.which('dnf'):
        return 'dnf install -y v4l2loopback akmod-v4l2loopback kernel-devel && (akmods --force || true)'
    if shutil.which('zypper'):
        return 'zypper --non-interactive install v4l2loopback-kmp-default v4l2loopback-utils'
    return ''
