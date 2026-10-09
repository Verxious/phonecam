from pathlib import Path
from PySide6.QtCore import QObject, Signal
from .android import Commands
from .driver import available, tool


class VirtualCamera(QObject):
    ready = Signal(str, str)
    error = Signal(str)
    driver_missing = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.commands = Commands(self)

    def prepare(self):
        for directory in Path('/sys/class/video4linux').glob('video*'):
            try:
                name = (directory / 'name').read_text().strip()
            except OSError:
                continue
            if name in ('PhoneCam', 'POCO Webcam'):
                self.ready.emit('/dev/' + directory.name, name)
                return
        if not available():
            self.driver_missing.emit()
            return
        number = next((number for number in range(11, 64) if not Path(f'/sys/class/video4linux/video{number}').exists()), None)
        if number is None:
            self.error.emit('Δεν υπάρχει διαθέσιμη θέση για virtual camera.')
            return
        device = f'/dev/video{number}'
        done = lambda code, output: self.error.emit(output or 'Δεν δημιουργήθηκε η virtual camera.') if code else self.ready.emit(device, 'PhoneCam')
        if not Path('/sys/module/v4l2loopback').exists():
            # Loading the driver with our device works on every v4l2loopback version.
            self.commands.run('pkexec', [tool('modprobe'), 'v4l2loopback', 'devices=1', f'video_nr={number}', 'card_label=PhoneCam', 'exclusive_caps=1'], done)
        else:
            # Driver already in use (e.g. by OBS): add a device next to it (v4l2loopback 0.13+).
            self.commands.run('pkexec', [tool('v4l2loopback-ctl'), 'add', '-n', 'PhoneCam', '-x', '1', device], done)

