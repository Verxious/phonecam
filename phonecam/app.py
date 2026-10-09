import argparse
from dataclasses import replace
import os
from pathlib import Path
import shutil
import signal
import sys
from PySide6.QtCore import QProcess, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSizePolicy, QSlider, QSpinBox, QSplitter, QVBoxLayout, QWidget)
from . import driver, library, scene, updater, weights, youtube
from .android import Android
from .config import Preferences
from .engine import CaptureEngine
from .network import StreamCheck, UsbStream
from .virtual import VirtualCamera

ROOT = Path(__file__).resolve().parent
LOOKS = [('Φυσικό', 'natural'), ('Ζεστό', 'warm'), ('Ασπρόμαυρο', 'mono'), ('Έντονα χρώματα', 'vivid')]
FITS = [('Αυτόματο · κάθετα ολόκληρα', 'auto'), ('Γέμισμα οθόνης', 'cover'), ('Ολόκληρο · μαύρες μπάρες', 'bars'), ('Ολόκληρο · θολές μπάρες', 'blur')]
COMMON_SIZES = [('720p', '1280x720'), ('1080p', '1920x1080'), ('1440p', '2560x1440'), ('4K', '3840x2160')]


class BackgroundJob(QThread):
    """Fetch the scene model if needed and render an animated loop once."""
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(object, str)

    def __init__(self, capture, parent=None):
        super().__init__(parent)
        self.capture = replace(capture)
        self.cancelled = False

    def run(self):
        width, height = map(int, self.capture.size.split('x'))
        try:
            def downloading(done, total):
                self.progress.emit(f'Λήψη μοντέλου σκηνής (μία φορά)… {done * 100 // total if total else done >> 20}{"%" if total else " MB"}')
            if scene.is_video(self.capture.background):
                scene.seamless(self.capture.background, width, height, self.capture.fps,
                    lambda done, total: self.progress.emit(f'Seamless loop για το βίντεο… {int(done * 100 / total)}%'),
                    lambda: self.cancelled, self.capture.fit)
                self.done.emit(self.capture)
                return
            model = weights.scene_model(downloading, lambda: self.cancelled)
            self.progress.emit('Ανάλυση εικόνας φόντου…')
            scene.render(self.capture.background, width, height, self.capture.fps, model,
                lambda done, total: self.progress.emit(f'Προετοιμασία κινούμενου φόντου… {done * 100 // total}%'),
                lambda: self.cancelled, self.capture.fit)
            self.done.emit(self.capture)
        except InterruptedError:
            pass
        except Exception as exc:  # A background must never take the camera down.
            fallback = 'Παίζει το αρχικό βίντεο.' if scene.is_video(self.capture.background) else 'Χρησιμοποιείται στατική εικόνα.'
            self.failed.emit(self.capture, f'Το κινούμενο φόντο δεν ετοιμάστηκε ({exc}). {fallback}')


class YouTubeJob(QThread):
    progress = Signal(str)
    done = Signal(str, str)
    failed = Signal(str)

    def __init__(self, link, parent=None):
        super().__init__(parent)
        self.link = link
        self.cancelled = False

    def run(self):
        try:
            self.done.emit(*youtube.download(self.link, self.progress.emit, lambda: self.cancelled))
        except InterruptedError:
            pass
        except Exception as exc:
            self.failed.emit(f'Το βίντεο δεν κατέβηκε: {exc}')


LIBRARY = '\x00library'


class SettingsDialog(QDialog):
    def __init__(self, capture, camera=None, parent=None):
        super().__init__(parent)
        self.capture = replace(capture)
        self.setWindowTitle('Ποιότητα εικόνας')
        self.setMinimumWidth(390)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.size = QComboBox()
        supported = camera.sizes if camera and camera.sizes else [size for _, size in COMMON_SIZES]
        for label, size in COMMON_SIZES:
            if size in supported:
                self.size.addItem(f'{label} · {size}', size)
        if capture.size not in [self.size.itemData(i) for i in range(self.size.count())] and capture.size in supported:
            self.size.addItem(capture.size, capture.size)
        if not self.size.count():
            for size in supported:
                self.size.addItem(size, size)
        self.size.setCurrentIndex(max(0, self.size.findData(capture.size)))
        self.fps = QComboBox()
        for fps in (15, 24, 30, 60):
            if not camera or not camera.fps or fps in camera.fps or fps == 60:
                label = f'{fps} FPS' + (' · δοκιμαστικό' if fps == 60 and camera and fps not in camera.fps else '')
                self.fps.addItem(label, fps)
        self.fps.setCurrentIndex(max(0, self.fps.findData(capture.fps)))
        self.bitrate = QSpinBox()
        self.bitrate.setRange(4, 60)
        self.bitrate.setSuffix(' Mbps')
        self.bitrate.setValue(capture.bitrate)
        self.bitrate.setEnabled(capture.source == 'android')
        form.addRow('Ανάλυση', self.size)
        form.addRow('Καρέ ανά δευτερόλεπτο', self.fps)
        form.addRow('Ποιότητα μετάδοσης', self.bitrate)
        layout.addLayout(form)
        note = QLabel('Τα 1080p / 30 FPS είναι καλή αρχή. Τα 60 FPS εξαρτώνται από την κάμερα.\n\nΚλείσε το βίντεο στο Discord πριν αλλάξεις ανάλυση. Αν μια ρύθμιση αποτύχει, επανέρχεται η προηγούμενη.')
        if capture.source == 'network':
            note.setText('Η πραγματική ποιότητα και τα FPS ορίζονται και στην εφαρμογή του κινητού. Εδώ επιλέγεις την έξοδο προς το PC.\n\nΚλείσε το βίντεο στο Discord πριν αλλάξεις ανάλυση.')
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self):
        return replace(self.capture, size=self.size.currentData(), fps=self.fps.currentData(), bitrate=self.bitrate.value())


class Window(QMainWindow):
    def __init__(self, mode=None, autostart=False, snapshot=None):
        super().__init__()
        self.preferences = Preferences()
        self.capture = self.preferences.capture
        self.android = Android(self.preferences, self)
        self.virtual = VirtualCamera(self)
        self.usb_stream = UsbStream(self)
        self.engine = CaptureEngine(self)
        self.devices = []
        self.cameras = []
        self.device_path = ''
        self.device_name = ''
        self.original_url = self.capture.url
        self.preparing = False
        self.autostart = autostart
        self.snapshot = snapshot
        self.snapshot_saved = False
        self.pixmap = None
        self.closing = False
        self.background_job = None
        self.youtube_job = None
        self.library_dialog = None
        library.TITLES.update(self.preferences.data.get('titles', {}))
        self.setWindowTitle('PhoneCam')
        self.setWindowIcon(QIcon(str(ROOT / 'assets' / 'phonecam.svg')))
        self.resize(1100, 730)
        self.setMinimumSize(870, 610)
        self.setStyleSheet('''
            QMainWindow, QDialog { background: #171a21; color: #edf0f6; }
            QWidget { color: #edf0f6; font-size: 13px; }
            QLabel#title { font-size: 26px; font-weight: 600; }
            QLabel#muted { color: #a4aebd; }
            QLabel#preview { background: #0a0c10; border: 1px solid #303745; border-radius: 12px; }
            QPushButton, QComboBox, QLineEdit, QSpinBox { background: #252b36; border: 1px solid #3d4655; border-radius: 6px; padding: 8px; }
            QPushButton:hover { background: #303a48; }
            QPushButton:checked { background: #235651; border-color: #61d6c4; }
            QPushButton#connect { background: #66d8c4; color: #10221f; font-weight: 600; }
            QPushButton:disabled, QComboBox:disabled { color: #76818f; }
            QPushButton#connect:disabled { background: #2b3a39; color: #76818f; }
            QListWidget { background: #12151b; border: 1px solid #303745; border-radius: 8px; padding: 4px; outline: 0; }
            QListWidget::item { padding: 6px; border-radius: 6px; color: #edf0f6; }
            QListWidget::item:hover { background: #1d232d; }
            QListWidget::item:selected { background: #235651; color: #ffffff; }
            QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
            QScrollBar::handle:vertical { background: #3d4655; border-radius: 4px; min-height: 30px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QComboBox QAbstractItemView { background: #252b36; selection-background-color: #326960; }
            QSlider::groove:horizontal { height: 5px; background: #343e4b; }
            QSlider::handle:horizontal { background: #66d8c4; width: 14px; margin: -5px 0; border-radius: 7px; }
        ''')
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(22, 18, 22, 18)
        title_row = QHBoxLayout()
        title = QLabel('PhoneCam')
        title.setObjectName('title')
        title_row.addWidget(title)
        title_row.addStretch()
        self.update_button = QPushButton('Νέα έκδοση · επανεκκίνηση')
        self.update_button.setObjectName('connect')
        self.update_button.setVisible(False)
        self.update_button.clicked.connect(self.install_update)
        title_row.addWidget(self.update_button)
        title_row.addSpacing(12)
        self.badge = QLabel('Χωρίς σύνδεση')
        self.badge.setObjectName('muted')
        title_row.addWidget(self.badge)
        outer.addLayout(title_row)
        splitter = QSplitter()
        outer.addWidget(splitter, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 12, 14, 0)
        self.preview = QLabel('Σύνδεσε το κινητό σου για ζωντανή εικόνα')
        self.preview.setObjectName('preview')
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(400, 250)
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        left_layout.addWidget(self.preview, 1)
        buttons = QHBoxLayout()
        for text, callback in [('↶ Αριστερά', lambda: self.rotate(-90)), ('Δεξιά ↷', lambda: self.rotate(90))]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        self.mirror = QPushButton('Mirror')
        self.mirror.setCheckable(True)
        self.mirror.setChecked(self.capture.mirror)
        self.mirror.clicked.connect(lambda checked: self.apply(replace(self.capture, mirror=checked)))
        buttons.addWidget(self.mirror)
        reset = QPushButton('Reset')
        reset.clicked.connect(self.reset_image)
        buttons.addWidget(reset)
        left_layout.addLayout(buttons)
        self.status = QLabel('Έτοιμο')
        self.status.setObjectName('muted')
        self.status.setWordWrap(True)
        left_layout.addWidget(self.status)
        splitter.addWidget(left)
        right = QWidget()
        right.setMinimumWidth(290)
        right.setMaximumWidth(365)
        panel = QVBoxLayout(right)
        panel.setContentsMargins(10, 12, 0, 0)
        panel.addWidget(QLabel('Πηγή κάμερας'))
        self.source = QComboBox()
        self.source.addItem('Android 12+ · Απευθείας κάμερα', 'android')
        self.source.addItem('Android 10 / iPhone · HTTP / RTSP', 'network')
        self.source.setCurrentIndex(max(0, self.source.findData(self.capture.source)))
        panel.addWidget(self.source)
        self.direct_fields = QWidget()
        direct = QVBoxLayout(self.direct_fields)
        direct.setContentsMargins(0, 0, 0, 0)
        self.connection = QComboBox()
        self.connection.addItem('USB · καλώδιο δεδομένων', 'usb')
        self.connection.addItem('Wi-Fi · ίδιο τοπικό δίκτυο', 'wifi')
        self.connection.setCurrentIndex(max(0, self.connection.findData(mode or self.preferences.data['connection'])))
        direct.addWidget(self.connection)
        panel.addWidget(self.direct_fields)
        self.network_fields = QWidget()
        net = QVBoxLayout(self.network_fields)
        net.setContentsMargins(0, 0, 0, 0)
        self.url = QLineEdit(self.original_url)
        self.url.setPlaceholderText('http://192.168.1.50:8080/video')
        self.url.setToolTip('Η πλήρης διεύθυνση βίντεο από την εφαρμογή του κινητού, π.χ. IP Webcam: http://IP:8080/video')
        net.addWidget(self.url)
        self.usb = QCheckBox('Μέσω USB / ADB · Android')
        self.usb.setChecked(bool(self.preferences.data.get('network_usb', False)))
        net.addWidget(self.usb)
        network_help = QLabel('Ξεκίνα τη ροή στην εφαρμογή του κινητού και βάλε τη διεύθυνση βίντεο. Στο Android με USB ενεργοποίησε USB debugging. Για iPhone χρειάζεται εφαρμογή HTTP/RTSP στο ίδιο δίκτυο.')
        network_help.setObjectName('muted')
        network_help.setWordWrap(True)
        net.addWidget(network_help)
        panel.addWidget(self.network_fields)
        device_row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        device_row.addWidget(self.device_combo, 1)
        self.refresh_button = QPushButton('↻')
        self.refresh_button.setToolTip('Ανανέωση κινητών')
        self.refresh_button.clicked.connect(lambda: self.android.refresh())
        device_row.addWidget(self.refresh_button)
        panel.addLayout(device_row)
        self.camera_combo = QComboBox()
        self.camera_combo.addItem('Η κύρια πίσω κάμερα', '0')
        self.camera_combo.currentIndexChanged.connect(self.camera_changed)
        panel.addWidget(self.camera_combo)
        self.connect_button = QPushButton('Σύνδεση')
        self.connect_button.setObjectName('connect')
        self.connect_button.clicked.connect(self.toggle_connection)
        panel.addWidget(self.connect_button)
        quality = QPushButton('Ποιότητα · ανάλυση / FPS')
        quality.clicked.connect(self.settings)
        panel.addWidget(quality)
        panel.addSpacing(12)
        panel.addWidget(QLabel('Φίλτρο εικόνας'))
        self.look = QComboBox()
        for label, value in LOOKS:
            self.look.addItem(label, value)
        self.look.setCurrentIndex(self.look.findData(self.capture.look))
        self.look.currentIndexChanged.connect(lambda: self.apply(replace(self.capture, look=self.look.currentData())))
        panel.addWidget(self.look)
        self.exposure_label = QLabel('Φωτεινότητα: ' + str(self.capture.exposure))
        panel.addWidget(self.exposure_label)
        self.exposure = QSlider(Qt.Orientation.Horizontal)
        self.exposure.setRange(-20, 20)
        self.exposure.setValue(self.capture.exposure)
        self.exposure_timer = QTimer(self)
        self.exposure_timer.setSingleShot(True)
        self.exposure_timer.timeout.connect(lambda: self.apply(replace(self.capture, exposure=self.exposure.value())))
        self.exposure.valueChanged.connect(self.exposure_changed)
        panel.addWidget(self.exposure)
        panel.addSpacing(12)
        panel.addWidget(QLabel('Φόντο'))
        self.background = QComboBox()
        self.background.setToolTip('Αντικαθιστά το δωμάτιό σου με εικόνα· εσύ μένεις μπροστά.')
        self.fill_backgrounds()
        self.background.activated.connect(self.background_chosen)
        panel.addWidget(self.background)
        self.motion = QCheckBox('Κινούμενα εφέ')
        self.motion.setChecked(self.capture.motion)
        self.motion.toggled.connect(lambda checked: self.apply(replace(self.capture, motion=checked)))
        panel.addWidget(self.motion)
        self.background_mirror = QCheckBox('Mirror φόντου')
        self.background_mirror.setToolTip('Γυρίζει μόνο το φόντο· το κουμπί Mirror γυρίζει μόνο εσένα.')
        self.background_mirror.setChecked(self.capture.background_mirror)
        self.background_mirror.toggled.connect(lambda checked: self.apply(replace(self.capture, background_mirror=checked)))
        panel.addWidget(self.background_mirror)
        fit_row = QHBoxLayout()
        fit_row.addWidget(QLabel('Κάδρο'))
        self.fit = QComboBox()
        self.fit.setToolTip('Πώς μπαίνει στην οθόνη ένα φόντο με άλλο σχήμα, π.χ. κάθετο βίντεο από κινητό.')
        for label, value in FITS:
            self.fit.addItem(label, value)
        self.fit.setCurrentIndex(max(0, self.fit.findData(self.capture.fit)))
        self.fit.currentIndexChanged.connect(lambda: self.apply(replace(self.capture, fit=self.fit.currentData())))
        fit_row.addWidget(self.fit, 1)
        panel.addLayout(fit_row)
        panel.addStretch()
        self.help = QLabel('Στο Discord / OBS επίλεξε τη virtual camera.\n\nUSB: ενεργοποίησε USB debugging και δέξου την άδεια στο κινητό. Wi-Fi: την πρώτη φορά άφησε το USB συνδεδεμένο.')
        self.help.setWordWrap(True)
        self.help.setObjectName('muted')
        panel.addWidget(self.help)
        splitter.addWidget(right)
        splitter.setSizes([735, 315])
        self.source.currentIndexChanged.connect(self.source_changed)
        self.usb.toggled.connect(self.update_fields)
        self.android.devices.connect(self.devices_found)
        self.android.cameras.connect(self.cameras_found)
        self.android.status.connect(self.status.setText)
        self.android.error.connect(self.error)
        self.virtual.ready.connect(self.virtual_ready)
        self.virtual.error.connect(self.error)
        self.virtual.driver_missing.connect(self.offer_driver)
        self.usb_stream.ready.connect(self.stream_ready)
        self.usb_stream.error.connect(self.error)
        self.engine.frame.connect(self.show_frame)
        self.engine.state.connect(self.state_changed)
        self.engine.failure.connect(self.error)
        self.engine.control_error.connect(self.status.setText)
        self.engine.working.connect(self.working)
        self.engine.restored.connect(self.restore)
        self.engine.measured_fps.connect(self.fps_measured)
        self.update_fields()
        self.sync_controls()
        QTimer.singleShot(0, self.android.refresh)
        # Look for a new version shortly after start, then every few hours.
        self.update_check = updater.UpdateCheck(self)
        self.update_check.available.connect(self.update_available)
        self.update_check.release.connect(self.release_available)
        self.release = None
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(lambda: self.update_check.isRunning() or self.update_check.start())
        self.update_timer.start(4 * 3600 * 1000)
        QTimer.singleShot(8000, self.update_check.start)

    def update_fields(self):
        direct = self.source.currentData() == 'android'
        self.direct_fields.setVisible(direct)
        self.network_fields.setVisible(not direct)
        self.device_combo.setVisible(direct or self.usb.isChecked())
        self.refresh_button.setVisible(direct or self.usb.isChecked())
        self.camera_combo.setVisible(direct)

    def source_changed(self):
        self.stop_capture()
        self.capture = replace(self.capture, source=self.source.currentData())
        self.update_fields()

    def devices_found(self, devices):
        self.devices = devices
        selected = self.device_combo.currentData() or self.capture.serial
        self.device_combo.clear()
        for device in devices:
            self.device_combo.addItem(device.label, device.serial)
        if not devices:
            self.device_combo.addItem('Δεν βρέθηκε κινητό', None)
        index = self.device_combo.findData(selected)
        if index >= 0:
            self.device_combo.setCurrentIndex(index)
        if self.autostart:
            self.autostart = False
            if devices or self.source.currentData() == 'network':
                self.connect_capture()

    def selected_device(self):
        return next((device for device in self.devices if device.serial == self.device_combo.currentData()), None)

    def toggle_connection(self):
        if self.engine.want_capture or self.preparing:
            self.stop_capture()
        else:
            self.connect_capture()

    def set_busy(self, busy):
        self.preparing = busy
        self.connect_button.setText('Ακύρωση' if busy else ('Αποσύνδεση' if self.engine.want_capture else 'Σύνδεση'))
        for widget in (self.source, self.connection, self.device_combo, self.url, self.usb, self.refresh_button):
            widget.setEnabled(not busy and not self.engine.want_capture)

    def connect_capture(self):
        # The virtual camera driver is checked (and offered) later; these must exist up front.
        missing = [name for name in ('adb', 'scrcpy', 'ffmpeg') if not shutil.which(name)]
        if missing:
            hint = 'Κατέβασε ξανά το AppImage.' if os.environ.get('APPIMAGE') else 'Τρέξε ./install.sh στον φάκελο του PhoneCam.'
            self.error('Λείπουν εργαλεία: ' + ', '.join(missing) + '. ' + hint)
            return
        self.capture = replace(self.capture, source=self.source.currentData())
        self.set_busy(True)
        if self.capture.source == 'android':
            device = self.selected_device()
            if not device:
                self.error('Σύνδεσε το κινητό με καλώδιο δεδομένων, ενεργοποίησε USB debugging και πάτησε Ανανέωση.')
                return
            mode = self.connection.currentData()
            self.preferences.data['connection'] = mode
            self.preferences.save()
            self.android.connect_device(device, mode)
        else:
            self.original_url = self.url.text().strip()
            self.preferences.data['network_usb'] = self.usb.isChecked()
            self.preferences.save()
            self.capture = replace(self.capture, url=self.original_url)
            try:
                self.capture.validated()
            except ValueError as exc:
                self.error(str(exc))
                return
            if self.usb.isChecked():
                device = self.selected_device()
                if not device or ':' in device.serial or device.state != 'device':
                    self.error('Διάλεξε εξουσιοδοτημένο Android συνδεδεμένο με USB.')
                    return
                self.capture = replace(self.capture, serial=device.serial)
                self.status.setText('Σύνδεση ροής μέσα από USB…')
                self.usb_stream.prepare(device.serial, self.original_url)
            else:
                self.stream_ready(self.original_url)

    def stream_ready(self, url):
        if not self.preparing:
            return
        self.status.setText('Έλεγχος ροής από το κινητό…')
        check = StreamCheck(url, self.usb.isChecked(), self)
        check.result.connect(self.stream_checked)
        check.finished.connect(check.deleteLater)
        self.stream_check = check
        check.start()

    def stream_checked(self, url, problem):
        if not self.preparing:
            return
        if problem:
            self.error(problem)
            self.usb_stream.release()
            return
        self.capture = replace(self.capture, url=url)
        self.virtual.prepare()

    def cameras_found(self, serial, cameras, name):
        if not self.preparing:
            return
        self.cameras = cameras
        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        for camera in cameras:
            self.camera_combo.addItem(camera.label, camera.identifier)
        index = self.camera_combo.findData(self.capture.camera)
        self.camera_combo.setCurrentIndex(max(0, index))
        self.camera_combo.blockSignals(False)
        camera = self.current_camera()
        self.capture = replace(self.capture, serial=serial, camera=camera.identifier)
        self.ensure_size(camera)
        self.status.setText(name + ' · Έτοιμη κάμερα')
        self.virtual.prepare()

    def current_camera(self):
        return next((camera for camera in self.cameras if camera.identifier == self.camera_combo.currentData()), None)

    def ensure_size(self, camera):
        if camera and camera.sizes and self.capture.size not in camera.sizes:
            size = next((size for _, size in reversed(COMMON_SIZES[:2]) if size in camera.sizes), camera.sizes[0])
            self.capture = replace(self.capture, size=size)

    def virtual_ready(self, path, name):
        if not self.preparing:
            return
        self.device_path, self.device_name = path, name
        self.help.setText(f'Στο Discord / OBS επίλεξε «{name}».\n\nΤα φίλτρα, η περιστροφή και το Mirror εφαρμόζονται και στην έξοδο της κάμερας.')
        self.prepare_background(self.capture)
        self.engine.start(self.capture, path)
        self.set_busy(False)

    def camera_changed(self):
        camera = self.current_camera()
        if camera:
            self.capture = replace(self.capture, camera=camera.identifier)
            self.ensure_size(camera)
            self.apply(self.capture)

    def fill_backgrounds(self):
        self.background.blockSignals(True)
        self.background.clear()
        self.background.addItem('Κανένα · πραγματικό δωμάτιο', '')
        recent = [path for path in self.preferences.data.get('backgrounds', []) if Path(path).is_file()]
        if self.capture.background and self.capture.background not in recent and Path(self.capture.background).is_file():
            recent.insert(0, self.capture.background)
        for path in recent:
            self.background.addItem(('▶ ' if library.downloaded(path) else '') + library.title(path), path)
        self.background.insertSeparator(self.background.count())
        self.background.addItem('Διαχείριση · προσθήκη φόντων…', LIBRARY)
        self.background.setCurrentIndex(max(0, self.background.findData(self.capture.background)))
        self.background.blockSignals(False)

    def remember_background(self, path):
        recent = [path] + [item for item in self.preferences.data.get('backgrounds', []) if item != path]
        self.preferences.data['backgrounds'] = recent[:30]
        self.preferences.save()
        self.fill_backgrounds()

    def background_chosen(self):
        path = self.background.currentData()
        if path == LIBRARY:
            self.fill_backgrounds()
            self.open_library()
            return
        self.use_background(path)

    def use_background(self, path):
        self.apply(replace(self.capture, background=path))
        self.fill_backgrounds()
        if self.library_dialog:
            self.library_dialog.refresh()

    def open_library(self):
        dialog = library.LibraryDialog(self)
        self.library_dialog = dialog
        dialog.exec()
        self.library_dialog = None
        dialog.deleteLater()

    def forget_background(self, path):
        self.preferences.data.get('titles', {}).pop(path, None)
        library.TITLES.pop(path, None)
        if path == self.capture.background:
            self.apply(replace(self.capture, background=''))
        self.preferences.data['backgrounds'] = [item for item in self.preferences.data.get('backgrounds', []) if item != path]
        self.preferences.save()
        self.fill_backgrounds()

    def download_link(self, link):
        if self.youtube_job and self.youtube_job.isRunning():
            self.youtube_job.cancelled = True
        job = YouTubeJob(link, self)
        job.progress.connect(self.download_progress)
        job.failed.connect(self.download_progress)
        job.done.connect(self.youtube_ready)
        job.finished.connect(lambda: self.youtube_finished(job))
        self.youtube_job = job
        self.download_progress('Λήψη βίντεο…')
        job.start()

    def download_progress(self, text):
        self.status.setText(text)
        if self.library_dialog:
            self.library_dialog.progress.setText(text)

    def youtube_finished(self, job):
        if self.youtube_job is job:
            self.youtube_job = None
        job.deleteLater()

    def remember_title(self, path, name):
        titles = self.preferences.data.setdefault('titles', {})
        titles[path] = name
        library.TITLES.update(titles)
        self.preferences.save()
        self.fill_backgrounds()

    def youtube_ready(self, path, name):
        self.remember_title(path, name)
        self.remember_background(path)
        self.download_progress('Το βίντεο κατέβηκε· ετοιμάζεται το seamless loop…')
        self.use_background(path)

    def prepare_background(self, capture):
        """Start rendering the loop in the background; the still image is used meanwhile."""
        if not capture.background or not capture.motion or not Path(capture.background).is_file():
            return
        width, height = map(int, capture.size.split('x'))
        mode = scene.resolve_fit(capture.background, capture.fit, width, height)
        if scene.cache_path(capture.background, width, height, capture.fps, mode).exists():
            return
        job = self.background_job
        if job and job.isRunning():
            if (job.capture.background, job.capture.size, job.capture.fps, job.capture.fit) == (capture.background, capture.size, capture.fps, capture.fit):
                return
            job.cancelled = True
        job = BackgroundJob(capture, self)
        job.progress.connect(self.status.setText)
        job.done.connect(self.background_ready)
        job.failed.connect(lambda _, message: self.status.setText(message))
        job.finished.connect(lambda: self.background_finished(job))
        self.background_job = job
        job.start()

    def background_finished(self, job):
        if self.background_job is job:
            self.background_job = None
        job.deleteLater()

    def background_ready(self, rendered):
        current = self.capture
        if (rendered.background, rendered.size, rendered.fps, rendered.fit) != (current.background, current.size, current.fps, current.fit) or not current.motion:
            return
        self.status.setText('Το κινούμενο φόντο είναι έτοιμο.')
        if self.engine.want_capture:
            self.engine.start(current, self.device_path)

    def apply(self, capture):
        self.capture = capture
        self.sync_controls()
        self.prepare_background(capture)
        if self.engine.want_capture:
            self.engine.start(capture, self.device_path)
        else:
            self.persist(capture)

    def rotate(self, amount):
        self.apply(replace(self.capture, rotation=(self.capture.rotation + amount) % 360))

    def reset_image(self):
        self.exposure_timer.stop()
        # Reset is about the picture; the chosen background stays.
        self.apply(replace(self.capture, rotation=0, mirror=False, look='natural', exposure=0))

    def exposure_changed(self, value):
        self.exposure_label.setText(f'Φωτεινότητα: {value}')
        self.exposure_timer.start(90)

    def sync_controls(self):
        video = bool(self.capture.background) and scene.is_video(self.capture.background)
        self.motion.setText('Seamless loop · χωρίς κόψιμο στην επανάληψη' if video else 'Κινούμενα εφέ')
        self.motion.setToolTip(
            'Όταν το βίντεο τελειώνει, τα τελευταία 1,5 δευτ. σβήνουν ομαλά μέσα στην αρχή του,\n'
            'ώστε να μη φαίνεται πού ξαναρχίζει. Ετοιμάζεται μία φορά στο μέγεθος της κάμερας,\n'
            'οπότε παίζει και πιο ελαφριά. Χωρίς αυτό, το βίντεο πηδάει απότομα στην αρχή.' if video else
            'Η εικόνα ζωντανεύει: σύννεφα, κύματα, ομίχλη και φως που τρεμοπαίζει.\nΕτοιμάζεται μία φορά (περίπου 1 λεπτό).')
        self.fit.blockSignals(True)
        self.fit.setCurrentIndex(max(0, self.fit.findData(self.capture.fit)))
        self.fit.blockSignals(False)
        for box, value in ((self.motion, self.capture.motion), (self.background_mirror, self.capture.background_mirror)):
            box.blockSignals(True)
            box.setChecked(value)
            box.blockSignals(False)
        index = self.background.findData(self.capture.background)
        if index >= 0 and index != self.background.currentIndex():
            self.background.blockSignals(True)
            self.background.setCurrentIndex(index)
            self.background.blockSignals(False)
        for widget, value in ((self.mirror, self.capture.mirror), (self.look, self.look.findData(self.capture.look)), (self.exposure, self.capture.exposure)):
            widget.blockSignals(True)
            if widget is self.mirror:
                widget.setChecked(value)
            elif widget is self.look:
                widget.setCurrentIndex(value)
            else:
                widget.setValue(value)
            widget.blockSignals(False)
        self.exposure_label.setText(f'Φωτεινότητα: {self.capture.exposure}')
        self.camera_combo.blockSignals(True)
        index = self.camera_combo.findData(self.capture.camera)
        if index >= 0:
            self.camera_combo.setCurrentIndex(index)
        self.camera_combo.blockSignals(False)

    def settings(self):
        dialog = SettingsDialog(self.capture, self.current_camera() if self.capture.source == 'android' else None, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.apply(dialog.selected())

    def persist(self, capture):
        saved = replace(capture, url=self.original_url) if capture.source == 'network' else capture
        try:
            self.preferences.save_capture(saved)
        except ValueError:
            # A network address can still be empty while setting up the form.
            pass

    def working(self, capture):
        self.capture = capture
        self.sync_controls()
        self.persist(capture)
        self.set_busy(False)
        self.badge.setText(f'{capture.size} · {capture.fps} FPS στόχος')

    def restore(self, capture):
        self.capture = capture
        self.sync_controls()
        self.status.setText('Η ρύθμιση απέτυχε — επαναφορά προηγούμενης εικόνας…')

    def fps_measured(self, fps):
        self.badge.setText(f'{self.capture.size} · {fps:.0f} FPS' + (f' / {self.capture.fps} στόχος' if fps < self.capture.fps - 3 else ''))

    def state_changed(self, text):
        self.status.setText(text)
        if not self.engine.want_capture:
            self.set_busy(False)

    def error(self, message):
        if self.closing:
            return
        self.set_busy(False)
        self.status.setText(message)
        self.badge.setText('Χωρίς σύνδεση')
        # Keep errors in the window so disconnects never trap the user in modals.
        if not self.engine.want_capture:
            self.usb_stream.release()

    def stop_capture(self):
        self.preparing = False
        self.android.generation += 1
        self.android.commands.cancel()
        self.virtual.commands.cancel()
        self.engine.stop()
        self.usb_stream.release()
        self.set_busy(False)
        self.badge.setText('Χωρίς σύνδεση')

    def show_frame(self, image):
        self.pixmap = QPixmap.fromImage(image)
        self.paint_preview()
        if self.snapshot and not self.snapshot_saved:
            self.snapshot_saved = True
            QTimer.singleShot(3500, lambda: self.grab().save(self.snapshot))

    def paint_preview(self):
        if self.pixmap:
            self.preview.setPixmap(self.pixmap.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'pixmap'):
            self.paint_preview()

    def offer_driver(self):
        script = driver.install_script()
        if not script:
            self.error('Λείπει ο driver της virtual camera (v4l2loopback). Εγκατάστησέ τον από τη διανομή σου και πάτησε ξανά Σύνδεση.')
            return
        answer = QMessageBox.question(self, 'Driver κάμερας',
            'Για να εμφανιστεί η κάμερα στο Discord / OBS χρειάζεται ο driver v4l2loopback (μία φορά, θέλει κωδικό διαχειριστή).\n\n'
            f'Θα εκτελεστεί:\n{script}\n\nΕγκατάσταση τώρα;')
        if answer != QMessageBox.StandardButton.Yes:
            self.error('Χωρίς τον driver v4l2loopback δεν γίνεται virtual camera.')
            return
        self.status.setText('Εγκατάσταση driver κάμερας… (μπορεί να πάρει 1–2 λεπτά)')
        def finished(code, output):
            if code == 0 and driver.available():
                self.status.setText('Ο driver εγκαταστάθηκε.')
                if self.preparing:
                    self.virtual.prepare()
            else:
                self.error('Η εγκατάσταση του driver απέτυχε.' + (('\n' + output.strip().splitlines()[-1][:300]) if output.strip() else ''))
        self.virtual.commands.run('pkexec', ['/bin/sh', '-c', script], finished)

    def update_available(self, changes):
        self.update_button.setToolTip('Αλλαγές:\n• ' + '\n• '.join(changes[:8]))
        self.update_button.setVisible(True)

    def release_available(self, version, url, notes):
        self.release = (version, url, notes)
        self.update_button.setText(f'Νέα έκδοση {version}')
        self.update_button.setToolTip('\n'.join(['Αλλαγές:', *('• ' + note for note in notes[:8])]) if notes else version)
        self.update_button.setVisible(True)

    def install_update(self):
        if self.release:
            version, url, notes = self.release
            text = f'Θα κατέβει η έκδοση {version} και η εφαρμογή θα ανοίξει ξανά· η κάμερα κόβεται για λίγα δευτερόλεπτα.'
            if notes:
                text += '\n\nΑλλαγές:\n• ' + '\n• '.join(notes[:8])
            if QMessageBox.question(self, 'Νέα έκδοση', text) != QMessageBox.StandardButton.Yes:
                return
            self.update_button.setEnabled(False)
            download = updater.AppImageDownload(url, self)
            download.progress.connect(lambda value: self.update_button.setText(f'Λήψη… {value}%'))
            download.failed.connect(lambda message: (self.status.setText(message), self.update_button.setEnabled(True), self.update_button.setText(f'Νέα έκδοση {version}')))
            download.done.connect(lambda: self.restart(os.environ['APPIMAGE']))
            download.start()
            self.download = download
            return
        changes = updater.pending()
        text = 'Η εφαρμογή θα κλείσει και θα ανοίξει ξανά με τη νέα έκδοση· η κάμερα κόβεται για λίγα δευτερόλεπτα.'
        if changes:
            text += '\n\nΑλλαγές:\n• ' + '\n• '.join(changes[:8])
        if QMessageBox.question(self, 'Νέα έκδοση', text) != QMessageBox.StandardButton.Yes:
            return
        self.restart(str(ROOT.parent / 'run.sh'))

    def restart(self, program):
        arguments = [argument for argument in sys.argv[1:] if not argument.startswith('--snapshot')]
        if self.engine.want_capture and '--autostart' not in arguments:
            arguments.append('--autostart')
        # Start after this instance has released the single-instance socket and camera.
        QProcess.startDetached('/bin/sh', ['-c', 'sleep 2; exec "$0" "$@"', program, *arguments])
        self.close()

    def closeEvent(self, event):
        self.closing = True
        self.exposure_timer.stop()
        self.update_timer.stop()
        self.update_check.wait(2000)
        if self.background_job and self.background_job.isRunning():
            self.background_job.cancelled = True
            self.background_job.wait(3000)
        self.android.generation += 1
        self.android.commands.cancel()
        self.virtual.commands.cancel()
        self.engine.close()
        self.usb_stream.close()
        event.accept()


def register_appimage():
    """An AppImage puts itself in the application menu, pointing at wherever it lives."""
    path = os.environ.get('APPIMAGE')
    if not path:
        return
    data = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share')))
    icon = data / 'icons/hicolor/scalable/apps/phonecam.svg'
    entry = data / 'applications/phonecam.desktop'
    quoted = '"' + path.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$') + '"'
    content = ('[Desktop Entry]\nType=Application\nName=PhoneCam\nComment=Το κινητό σου ως webcam · USB / Wi-Fi / HTTP / RTSP\n'
        f'Exec={quoted} --autostart\nIcon=phonecam\nTerminal=false\nCategories=AudioVideo;Video;\nStartupNotify=false\n')
    try:
        icon.parent.mkdir(parents=True, exist_ok=True)
        if not icon.exists():
            shutil.copyfile(ROOT / 'assets' / 'phonecam.svg', icon)
        entry.parent.mkdir(parents=True, exist_ok=True)
        if not entry.exists() or entry.read_text() != content:
            entry.write_text(content)
    except OSError:
        pass


def self_test():
    """Headless check of everything an install needs (used to test builds on other distros)."""
    import ctypes
    import subprocess
    import numpy
    from .compositor import Matte
    from .live import zmq_library
    results = {}
    def check(name, test):
        try:
            results[name] = test() or 'ok'
        except Exception as exc:
            results[name] = f'FAIL {exc}'
    check('ffmpeg zmq', lambda: None if b' zmq ' in subprocess.run(['ffmpeg', '-hide_banner', '-filters'], capture_output=True).stdout else 1 / 0)
    check('scrcpy', lambda: subprocess.run(['scrcpy', '--version'], capture_output=True, text=True).stdout.split()[1])
    check('adb', lambda: subprocess.run(['adb', 'version'], capture_output=True, text=True).stdout.split('\n')[0])
    check('libzmq', lambda: ctypes.CDLL(zmq_library()).zmq_ctx_new and zmq_library())
    def https():
        import urllib.request
        with urllib.request.urlopen('https://api.github.com', timeout=15) as response:
            return f'ok ({response.status})'
    check('https', https)
    check('matte', lambda: str(Matte(320, 180)(numpy.zeros((180, 320, 3), numpy.uint8)).shape))
    check('driver', lambda: 'installed' if driver.available() else 'missing (offered in app): ' + (driver.install_script() or 'unknown distro'))
    def window():
        app = QApplication.instance() or QApplication(['phonecam'])
        widget = Window()
        widget.android.refresh = lambda *_: None
        widget.close()
        return app.platformName()
    check('qt window', window)
    for name, value in results.items():
        print(f'{name}: {value}')
    return 1 if any(str(value).startswith('FAIL') for value in results.values()) else 0


def main():
    parser = argparse.ArgumentParser(description='PhoneCam — phone camera for Linux')
    parser.add_argument('--mode', choices=('usb', 'wifi'))
    parser.add_argument('--autostart', action='store_true')
    parser.add_argument('--snapshot', help=argparse.SUPPRESS)
    parser.add_argument('--self-test', action='store_true', help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.self_test:
        return self_test()
    register_appimage()
    app = QApplication(sys.argv[:1])
    app.setApplicationName('PhoneCam')
    app.setDesktopFileName('phonecam')
    socket_name = f'phonecam-{os.getuid()}'
    socket = QLocalSocket()
    socket.connectToServer(socket_name)
    if socket.waitForConnected(350):
        socket.write(b'raise')
        socket.waitForBytesWritten(500)
        return 0
    QLocalServer.removeServer(socket_name)
    server = QLocalServer()
    if not server.listen(socket_name):
        print('Δεν ξεκίνησε ο έλεγχος δεύτερης εκκίνησης.', file=sys.stderr)
        return 1
    window = Window(arguments.mode, arguments.autostart, arguments.snapshot)

    def activate():
        connection = server.nextPendingConnection()
        if connection:
            connection.disconnectFromServer()
            connection.deleteLater()
        window.showNormal()
        window.raise_()
        window.activateWindow()

    server.newConnection.connect(activate)
    signal.signal(signal.SIGTERM, lambda *_: window.close())
    signal.signal(signal.SIGINT, lambda *_: window.close())
    timer = QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(250)
    window.show()
    return app.exec()
