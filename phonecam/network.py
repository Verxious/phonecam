from urllib.parse import urlsplit, urlunsplit
from PySide6.QtCore import QObject, Signal
from .android import Commands


def forwarded_url(url, local_port):
    parsed = urlsplit(url)
    # Preserve credentials and the application endpoint when replacing the host.
    credentials = parsed.netloc.rsplit('@', 1)[0] + '@' if '@' in parsed.netloc else ''
    return urlunsplit((parsed.scheme, credentials + f'127.0.0.1:{local_port}', parsed.path, parsed.query, parsed.fragment))


class UsbStream(QObject):
    ready = Signal(str)
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.commands = Commands(self)
        self.forward = None
        self.generation = 0

    def prepare(self, serial, url):
        self.release()
        generation = self.generation
        try:
            parsed = urlsplit(url)
            port = parsed.port or {'http': 80, 'https': 443, 'rtsp': 554, 'rtsps': 322}[parsed.scheme]
        except (ValueError, KeyError):
            self.error.emit('Μη έγκυρη διεύθυνση ή θύρα ροής.')
            return

        def done(code, output):
            try:
                local_port = int(output.strip()) if not code else 0
            except ValueError:
                local_port = 0
            if generation != self.generation:
                if local_port:
                    self.commands.run('adb', ['-s', serial, 'forward', '--remove', f'tcp:{local_port}'], lambda *_: None)
                return
            if not local_port:
                self.error.emit('Δεν δημιουργήθηκε η σύνδεση USB. Έλεγξε το USB debugging και την άδεια στο κινητό.')
                return
            self.forward = (serial, local_port)
            self.ready.emit(forwarded_url(url, local_port))

        self.commands.run('adb', ['-s', serial, 'forward', 'tcp:0', f'tcp:{port}'], done)

    def release(self):
        self.generation += 1
        if self.forward:
            serial, port = self.forward
            self.commands.run('adb', ['-s', serial, 'forward', '--remove', f'tcp:{port}'], lambda *_: None)
            self.forward = None

    def close(self):
        self.release()
        # Let adb remove mappings before the event loop exits.
        for process in list(self.commands.running):
            process.waitForFinished(1000)
        self.commands.cancel()
