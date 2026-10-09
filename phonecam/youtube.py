"""YouTube (and other sites yt-dlp supports) as a background: downloaded once, then looped."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request
from .config import STATE

DATA = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'phonecam'
OWN = DATA / 'bin' / 'yt-dlp'
RELEASE = 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux'
FOLDER = STATE / 'backgrounds' / 'youtube'
# Video only, at most 1080p; H.264 first because it decodes cheapest during calls.
FORMAT = 'bv*[height<=1080][vcodec^=avc1]/bv*[height<=1080]/b[height<=1080]/b'


def looks_like_link(text):
    return bool(re.match(r'https?://\S+$', text.strip()))


def program(progress=None):
    """A working yt-dlp: the system one, or our own copy (fetched and kept fresh)."""
    found = shutil.which('yt-dlp')
    if found and not str(found).startswith(str(OWN.parent)):
        return found
    if OWN.exists():
        # Sites change often; refresh our own copy weekly.
        if time.time() - OWN.stat().st_mtime > 7 * 86400:
            subprocess.run([str(OWN), '-U'], capture_output=True, timeout=120)
            OWN.touch()
        return str(OWN)
    if progress:
        progress('Λήψη yt-dlp (μία φορά)…')
    OWN.parent.mkdir(parents=True, exist_ok=True)
    partial = OWN.with_suffix('.part')
    request = urllib.request.Request(RELEASE, headers={'User-Agent': 'PhoneCam'})
    with urllib.request.urlopen(request, timeout=60) as response, open(partial, 'wb') as handle:
        shutil.copyfileobj(response, handle)
    partial.chmod(0o755)
    partial.replace(OWN)
    return str(OWN)


def download(link, progress=None, cancelled=None):
    """Download the video (no audio) and return its local path."""
    tool = program(progress)
    FOLDER.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([
        tool, '--no-playlist', '--newline', '--no-part', '--restrict-filenames', '-f', FORMAT,
        '--merge-output-format', 'mp4', '-o', str(FOLDER / '%(title).70B [%(id)s].%(ext)s'),
        '--print', 'after_move:filepath', link,
    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    path, errors = '', []
    try:
        for line in process.stdout:
            if cancelled and cancelled():
                raise InterruptedError
            line = line.strip()
            match = re.search(r'\[download\]\s+([\d.]+)%', line)
            if match and progress:
                progress(f'Λήψη βίντεο από YouTube… {float(match[1]):.0f}%')
            elif line.startswith('/') and Path(line).exists():
                path = line
            elif 'ERROR' in line:
                errors.append(line.split('ERROR:', 1)[-1].strip())
        if process.wait() != 0 or not path:
            raise RuntimeError(errors[-1][:200] if errors else 'δεν κατέβηκε το βίντεο')
        return path
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
