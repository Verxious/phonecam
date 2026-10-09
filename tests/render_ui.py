"""Render setup and quality UI without accessing a phone or webcam."""
import os
from pathlib import Path
import sys
import tempfile
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['XDG_CONFIG_HOME'] = tempfile.mkdtemp(prefix='phonecam-ui-')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from phonecam.app import Window, SettingsDialog
from phonecam.android import Android

Android.refresh = lambda *_: None
app = QApplication([])
window = Window()
window.source.setCurrentIndex(1)
window.url.setText('http://127.0.0.1:8080/video')
window.usb.setChecked(True)
window.show()
dialog = SettingsDialog(window.capture, None, window)


def setup_snapshot():
    window.grab().save('/tmp/phonecam-network-ui.png')
    dialog.show()
    QTimer.singleShot(100, finish)


def finish():
    dialog.grab().save('/tmp/phonecam-quality-ui.png')
    dialog.close()
    window.close()


QTimer.singleShot(100, setup_snapshot)
app.exec()
