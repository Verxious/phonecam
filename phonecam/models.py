from dataclasses import asdict, dataclass, field
import re
from urllib.parse import urlsplit


@dataclass
class Device:
    serial: str
    state: str
    model: str = ''

    @property
    def label(self):
        transport = 'Wi-Fi' if ':' in self.serial else 'USB'
        status = '' if self.state == 'device' else f' · {self.state}'
        return f'{self.model or self.serial} · {transport}{status}'


@dataclass
class Camera:
    identifier: str
    facing: str
    sensor_size: str
    fps: list[int] = field(default_factory=list)
    sizes: list[str] = field(default_factory=list)

    @property
    def label(self):
        facing = {'back': 'Πίσω', 'front': 'Μπροστινή', 'external': 'Εξωτερική'}.get(self.facing, self.facing)
        return f'{facing} · Κάμερα {self.identifier} · {self.sensor_size}'


@dataclass
class Capture:
    source: str = 'android'
    serial: str = ''
    url: str = ''
    camera: str = '0'
    size: str = '1920x1080'
    fps: int = 30
    bitrate: int = 20
    rotation: int = 0
    mirror: bool = False
    look: str = 'natural'
    exposure: int = 0
    background: str = ''
    motion: bool = True
    background_mirror: bool = False
    fit: str = 'auto'

    def validated(self):
        if self.source not in ('android', 'network'):
            raise ValueError('Άγνωστη πηγή κάμερας.')
        if not re.fullmatch(r'[1-9][0-9]{1,3}x[1-9][0-9]{1,3}', self.size):
            raise ValueError('Μη έγκυρη ανάλυση.')
        width, height = map(int, self.size.split('x'))
        if width > 8192 or height > 8192 or width % 2 or height % 2:
            raise ValueError('Μη έγκυρη ανάλυση.')
        if self.fps not in (15, 24, 30, 60) or self.bitrate not in range(4, 61):
            raise ValueError('Μη έγκυρα FPS ή ποιότητα.')
        if self.rotation not in (0, 90, 180, 270):
            raise ValueError('Μη έγκυρη περιστροφή.')
        if self.look not in ('natural', 'warm', 'mono', 'vivid') or self.exposure not in range(-20, 21):
            raise ValueError('Μη έγκυρο φίλτρο ή φωτεινότητα.')
        if not isinstance(self.background, str) or not isinstance(self.motion, bool) or not isinstance(self.background_mirror, bool):
            raise ValueError('Μη έγκυρο φόντο.')
        if self.fit not in ('auto', 'cover', 'bars', 'blur'):
            raise ValueError('Μη έγκυρο κάδρο φόντου.')
        if self.source == 'network':
            parsed = urlsplit(self.url)
            if parsed.scheme not in ('rtsp', 'rtsps', 'http', 'https') or not parsed.hostname:
                raise ValueError('Βάλε πλήρη διεύθυνση RTSP ή HTTP(S) της ροής κάμερας.')
        return self

    @property
    def orientation(self):
        # scrcpy flips before rotating: reverse the angle to mirror the final image.
        return f'flip{(360 - self.rotation) % 360}' if self.mirror else str(self.rotation)

    def to_dict(self):
        return asdict(self)


def parse_devices(output):
    result = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] not in ('device', 'unauthorized', 'offline', 'recovery'):
            continue
        values = dict(item.split(':', 1) for item in parts[2:] if ':' in item)
        result.append(Device(parts[0], parts[1], values.get('model', '').replace('_', ' ')))
    return result


def parse_cameras(output):
    cameras = []
    current = None
    high_speed = False
    for line in output.splitlines():
        match = re.search(r'--camera-id=(\S+)\s+\((\w+),\s*(\d+x\d+),\s*fps=\{([^}]*)\}', line)
        if match:
            identifier, facing, size, fps = match.groups()
            current = Camera(identifier, facing, size, [int(value) for value in re.findall(r'\d+', fps)])
            cameras.append(current)
            high_speed = False
        elif current and 'High speed capture' in line:
            high_speed = True
        elif current and not high_speed:
            match = re.match(r'\s*-\s*(\d+x\d+)\s*$', line)
            if match and match[1] not in current.sizes:
                current.sizes.append(match[1])
    return cameras
