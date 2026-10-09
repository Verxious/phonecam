import re
from PySide6.QtCore import QObject, QProcess, QTimer, Signal
from .models import parse_cameras, parse_devices


class Commands(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = []

    def run(self, program, arguments, callback, timeout=12000):
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.running.append(process)
        timer = QTimer(process)
        timer.setSingleShot(True)
        timed_out = [False]

        def expire():
            timed_out[0] = True
            process.kill()

        def finish(code, _):
            timer.stop()
            output = bytes(process.readAllStandardOutput()).decode(errors='replace')
            if timed_out[0]:
                output = 'Η εντολή καθυστέρησε υπερβολικά. Έλεγξε τη σύνδεση.'
                code = 1
            if process in self.running:
                self.running.remove(process)
                callback(code, output)
            process.deleteLater()

        def error(kind):
            if kind == QProcess.ProcessError.FailedToStart:
                finish(1, None)

        process.finished.connect(finish)
        process.errorOccurred.connect(error)
        timer.timeout.connect(expire)
        process.start(program, arguments)
        timer.start(timeout)
        return process

    def cancel(self):
        for process in list(self.running):
            process.blockSignals(True)
            process.kill()
            process.waitForFinished(500)
            process.deleteLater()
        self.running.clear()


class Android(QObject):
    devices = Signal(object)
    cameras = Signal(str, object, str)
    status = Signal(str)
    error = Signal(str)

    def __init__(self, preferences, parent=None):
        super().__init__(parent)
        self.preferences = preferences
        self.commands = Commands(self)
        self.generation = 0

    def refresh(self, retry=True):
        def done(code, output):
            devices = parse_devices(output)
            if not code and not devices and retry and self.preferences.data['endpoints']:
                endpoint = self.preferences.data['endpoints'][-1]
                self.commands.run('adb', ['connect', endpoint], lambda *_: self.refresh(False), timeout=6000)
            elif code:
                self.error.emit(output[-600:] or 'Δεν ξεκίνησε το adb.')
            else:
                self.devices.emit(devices)
        self.commands.run('adb', ['devices', '-l'], done)

    def connect_device(self, device, mode):
        self.generation += 1
        generation = self.generation
        if device.state != 'device':
            self.error.emit('Ενεργοποίησε USB debugging και πάτησε «Να επιτρέπεται» στο κινητό. Μετά πάτησε Ανανέωση.')
            return
        self.status.emit('Έλεγχος Android και κάμερας…')

        def properties(code, output):
            if generation != self.generation:
                return
            values = output.strip().splitlines()
            try:
                sdk = int(values[-1])
            except (ValueError, IndexError):
                sdk = 0
            if code or sdk < 31:
                self.error.emit('Η απευθείας κάμερα χρειάζεται Android 12 ή νεότερο. Για παλαιότερο κινητό μπορείς να χρησιμοποιήσεις Ροή δικτύου.')
                return
            name = (values[0].strip() if len(values) > 2 else '') or device.model or device.serial
            if mode == 'wifi' and ':' not in device.serial:
                self.prepare_wifi(device.serial, lambda serial: self.inspect(serial, name, generation))
            elif mode == 'usb' and ':' in device.serial:
                self.error.emit('Σύνδεσε το κινητό με καλώδιο δεδομένων και διάλεξε τη σύνδεση USB από τη λίστα.')
            else:
                self.inspect(device.serial, name, generation)

        self.commands.run('adb', ['-s', device.serial, 'shell', 'getprop ro.product.marketname; getprop ro.product.model; getprop ro.build.version.sdk'], properties)

    def prepare_wifi(self, serial, ready):
        generation = self.generation
        def address(code, output):
            if generation != self.generation:
                return
            match = re.search(r'\binet\s+(\d+\.\d+\.\d+\.\d+)/', output)
            if code or not match:
                self.error.emit('Σύνδεσε το κινητό στο ίδιο τοπικό δίκτυο με το PC και ξαναδοκίμασε.')
                return
            endpoint = match[1] + ':5555'

            def enabled(code, output):
                if generation != self.generation:
                    return
                if code:
                    self.error.emit(output[-600:])
                    return
                self.try_endpoint(endpoint, ready, generation=generation)

            self.status.emit('Ρύθμιση Wi-Fi — άφησε το USB συνδεδεμένο…')
            self.commands.run('adb', ['-s', serial, 'tcpip', '5555'], enabled)
        self.commands.run('adb', ['-s', serial, 'shell', 'ip', '-o', '-4', 'addr', 'show', 'wlan0'], address)

    def try_endpoint(self, endpoint, ready, attempt=0, generation=None):
        generation = self.generation if generation is None else generation
        if generation != self.generation:
            return
        def connected(code, output):
            if generation != self.generation:
                return
            def checked(code, output):
                if generation != self.generation:
                    return
                if not code and output.strip() == 'device':
                    endpoints = self.preferences.data['endpoints']
                    if endpoint not in endpoints:
                        endpoints.append(endpoint)
                        self.preferences.save()
                    ready(endpoint)
                elif attempt < 3:
                    QTimer.singleShot(1000, lambda: self.try_endpoint(endpoint, ready, attempt + 1, generation))
                else:
                    self.error.emit('Δεν συνδέθηκε μέσω Wi-Fi. Έλεγξε το δίκτυο και την άδεια debugging στο κινητό.')
            self.commands.run('adb', ['-s', endpoint, 'get-state'], checked, timeout=5000)
        self.commands.run('adb', ['connect', endpoint], connected, timeout=5000)

    def inspect(self, serial, name, generation):
        def done(code, output):
            if generation != self.generation:
                return
            cameras = parse_cameras(output)
            if code or not cameras:
                self.error.emit(output[-700:] or 'Δεν βρέθηκαν κάμερες στο κινητό.')
            else:
                self.cameras.emit(serial, cameras, name)
        self.commands.run('scrcpy', ['--serial', serial, '--list-cameras', '--list-camera-sizes'], done, timeout=18000)
