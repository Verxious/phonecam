import json
import os
from pathlib import Path
from .models import Capture

STATE = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'phonecam'


class Preferences:
    def __init__(self):
        STATE.mkdir(parents=True, exist_ok=True)
        self.path = STATE / 'settings.json'
        self.data = {'capture': Capture().to_dict(), 'connection': 'wifi', 'endpoints': []}
        try:
            self.data.update(json.loads(self.path.read_text()))
        except (OSError, ValueError, TypeError):
            self.migrate_legacy()

    def migrate_legacy(self):
        legacy = STATE.parent / 'poco-webcam'
        capture = Capture()
        try:
            size, fps, bitrate = (legacy / 'capture').read_text().split()
            capture.size, capture.fps, capture.bitrate = size, int(fps), int(bitrate)
        except (OSError, ValueError):
            pass
        try:
            capture.rotation = int((legacy / 'rotation').read_text())
            capture.mirror = (legacy / 'mirror').read_text().strip() == '1'
        except (OSError, ValueError):
            pass
        try:
            endpoint = (legacy / 'wifi-endpoint').read_text().strip()
            if endpoint:
                capture.serial = endpoint
                self.data['endpoints'] = [endpoint]
        except OSError:
            pass
        try:
            self.data['capture'] = capture.validated().to_dict()
        except ValueError:
            pass

    @property
    def capture(self):
        try:
            return Capture(**self.data['capture']).validated()
        except (TypeError, ValueError):
            return Capture()

    def save_capture(self, capture):
        self.data['capture'] = capture.validated().to_dict()
        self.save()

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + '\n')
        temporary.chmod(0o600)
        temporary.replace(self.path)
