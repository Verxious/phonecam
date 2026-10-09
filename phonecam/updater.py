"""Updates through git: fetch quietly in the background, apply on the next start.

run.sh fast-forwards to the fetched version before Python starts, so running code is
never replaced underneath the app (the compositor is a separate process).
"""
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request
from PySide6.QtCore import QThread, Signal

ROOT = Path(__file__).resolve().parents[1]


def git(*arguments, timeout=30):
    environment = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='true', LC_ALL='C')
    return subprocess.run(['git', '-C', str(ROOT), *arguments], capture_output=True, text=True, timeout=timeout, env=environment)


def enabled():
    return (ROOT / '.git').exists() and git('rev-parse', '--abbrev-ref', '@{u}', timeout=5).returncode == 0


def pending():
    """Commit subjects that are fetched but not yet applied, newest first."""
    result = git('log', '--format=%s', 'HEAD..@{u}', timeout=5)
    return [line for line in result.stdout.splitlines() if line.strip()] if result.returncode == 0 else []


class UpdateCheck(QThread):
    available = Signal(list)
    release = Signal(str, str, list)

    def run(self):
        if appimage():
            try:
                found = latest_release()
            except (OSError, ValueError, KeyError, urllib.error.URLError):
                return
            if found:
                self.release.emit(*found)
            return
        try:
            if not enabled():
                return
            git('fetch', '--quiet', 'origin')
            # Only offer versions run.sh can fast-forward to.
            if git('merge-base', '--is-ancestor', 'HEAD', '@{u}', timeout=5).returncode != 0:
                return
            changes = pending()
            if changes:
                self.available.emit(changes)
        except (OSError, subprocess.SubprocessError):
            pass


# --- AppImage builds update from GitHub releases instead of git ---------------

def appimage():
    from . import release
    path = os.environ.get('APPIMAGE', '')
    # Replacing the file needs write access to its folder, not just to the file.
    return path if release.APPIMAGE and release.REPOSITORY and path and os.access(os.path.dirname(path) or '.', os.W_OK) else ''


def version_tuple(text):
    return tuple(int(part) for part in re.findall(r'\d+', text)[:3])


def latest_release():
    """(version, AppImage URL, notes) of the newest GitHub release, if newer than this one."""
    from . import __version__, release
    request = urllib.request.Request(f'https://api.github.com/repos/{release.REPOSITORY}/releases/latest', headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'PhoneCam'})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.load(response)
    tag = data.get('tag_name', '')
    asset = next((item['browser_download_url'] for item in data.get('assets', []) if item.get('name', '').endswith('x86_64.AppImage')), '')
    if not asset or version_tuple(tag) <= version_tuple(__version__):
        return None
    notes = [line.strip('-* ').strip() for line in (data.get('body') or '').splitlines() if line.strip()]
    return tag, asset, notes


class AppImageDownload(QThread):
    progress = Signal(int)
    done = Signal()
    failed = Signal(str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = url

    def run(self):
        target = appimage()
        partial = target + '.part'
        try:
            request = urllib.request.Request(self.url, headers={'User-Agent': 'PhoneCam'})
            with urllib.request.urlopen(request, timeout=30) as response, open(partial, 'wb') as handle:
                total = int(response.headers.get('Content-Length') or 0)
                done = 0
                while block := response.read(1 << 20):
                    handle.write(block)
                    done += len(block)
                    if total:
                        self.progress.emit(done * 100 // total)
            with open(partial, 'rb') as handle:
                if handle.read(4) != b'\x7fELF':
                    raise ValueError('το αρχείο δεν είναι AppImage')
            os.chmod(partial, 0o755)
            os.replace(partial, target)  # The running copy stays valid until it exits.
            self.done.emit()
        except (OSError, ValueError) as exc:
            self.failed.emit(f'Η ενημέρωση απέτυχε: {exc}')
        finally:
            if os.path.exists(partial):
                os.unlink(partial)
