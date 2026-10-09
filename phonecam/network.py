import socket
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from PySide6.QtCore import QObject, QThread, Signal
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


def diagnose(url, usb=False, timeout=5):
    """Try the stream address before capture starts; '' if it looks like video, else why not."""
    parsed = urlsplit(url)
    try:
        port = parsed.port or {'http': 80, 'https': 443, 'rtsp': 554, 'rtsps': 322}[parsed.scheme]
    except (ValueError, KeyError):
        return 'Μη έγκυρη διεύθυνση ή θύρα ροής.'
    host = parsed.hostname or ''
    app_hint = 'Άνοιξε την εφαρμογή κάμερας στο κινητό και ξεκίνα τον server της (IP Webcam: «Start server»).'
    try:
        socket.create_connection((host, port), timeout).close()
    except ConnectionRefusedError:
        return f'Το κινητό απαντά, αλλά καμία εφαρμογή δεν ακούει στη θύρα {port}. {app_hint}'
    except socket.gaierror:
        return f'Δεν βρέθηκε η διεύθυνση «{host}». Έλεγξε ότι την έγραψες σωστά.'
    except OSError:
        if usb:
            return f'Δεν απαντά η εφαρμογή μέσω USB. {app_hint}'
        return (f'Δεν φτάνω το κινητό στο {host}. Κινητό και PC πρέπει να είναι στο ίδιο Wi-Fi '
                '(όχι Wi-Fi επισκεπτών)· η IP του κινητού μπορεί να άλλαξε — δες ξανά τη διεύθυνση στην εφαρμογή.')
    if parsed.scheme not in ('http', 'https'):
        return ''  # RTSP: reachable is all we can cheaply tell.
    request = urllib.request.Request(url, headers={'User-Agent': 'PhoneCam'})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl._create_unverified_context()) as response:
            kind = response.headers.get('Content-Type', '').lower()
            if 'text/html' in kind:
                suggestion = urlunsplit((parsed.scheme, parsed.netloc, '/video', '', '')) if parsed.path in ('', '/') else ''
                return ('Αυτή είναι η σελίδα της εφαρμογής, όχι το βίντεο. Βάλε τη διεύθυνση βίντεο'
                        + (f', π.χ. {suggestion}' if suggestion else ' (IP Webcam: …:8080/video)') + '.')
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return 'Η εφαρμογή ζητά κωδικό. Γράψε τον στη διεύθυνση: http://χρήστης:κωδικός@IP:θύρα/video'
        if error.code == 404:
            return f'Η διεύθυνση {parsed.path or "/"} δεν υπάρχει στην εφαρμογή. Για IP Webcam χρησιμοποίησε …:{port}/video'
        return f'Η εφαρμογή απάντησε με σφάλμα {error.code}.'
    except (OSError, urllib.error.URLError):
        pass  # A slow first byte is normal for some cameras; let FFmpeg try.
    return ''


class StreamCheck(QThread):
    result = Signal(str, str)  # url, problem ('' = fine)

    def __init__(self, url, usb, parent=None):
        super().__init__(parent)
        self.url, self.usb = url, usb

    def run(self):
        self.result.emit(self.url, diagnose(self.url, self.usb))
