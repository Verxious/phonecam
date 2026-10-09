"""Soundboard tab: DJ-style pads for sounds from links or files, trimmed to the seconds you choose."""
from dataclasses import replace
from pathlib import Path
import subprocess
import time
import numpy as np
from PySide6.QtCore import QPointF, QRectF, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QColorDialog, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSlider, QVBoxLayout, QWidget)
from . import scene, sound, youtube

AUDIO = 'Ήχοι και βίντεο (*.mp3 *.ogg *.opus *.wav *.flac *.m4a *.aac *.webm *.mp4 *.mkv);;Όλα (*)'
PALETTE = ['#ff4d6d', '#ff9f1c', '#ffd60a', '#52d273', '#2ec4b6', '#4ea8de', '#7b61ff', '#e056fd',
           '#ff6b6b', '#f4a261', '#90be6d', '#43aa8b', '#577590', '#f15bb5', '#00bbf9', '#9b5de5']
KEYS = list('1234567890QWERTYUIOPASDFGHJKLZXCVBNM')
# Physical key positions (X11/evdev scan codes), so pads answer the same keys whatever the
# keyboard language is: on a Greek layout Q is «;» but it is still the same key.
SCANCODES = {code: index for index, code in enumerate([*range(10, 20), *range(24, 34), *range(38, 47), *range(52, 59)])}
COLUMNS = 4


def clock(seconds):
    minutes, seconds = divmod(max(0.0, seconds), 60)
    return f'{int(minutes)}:{seconds:04.1f}'


def length_of(path):
    try:
        return scene.duration(path)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def colour(item, index):
    return QColor(item.color or PALETTE[index % len(PALETTE)])


class Download(QThread):
    progress = Signal(str)
    done = Signal(str, str, str)  # path, title, link
    failed = Signal(str)

    def __init__(self, link, parent=None):
        super().__init__(parent)
        self.link = link

    def run(self):
        try:
            path, title = youtube.download(self.link, self.progress.emit, audio=True)
            self.done.emit(path, title, self.link)
        except Exception as exc:
            self.failed.emit(f'Ο ήχος δεν κατέβηκε: {exc}')


class Peaks(QThread):
    """Loudness envelope for the waveform: FFmpeg decodes to a few thousand points."""
    ready = Signal(object)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self):
        try:
            result = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', self.path, '-vn', '-ac', '1',
                '-ar', '2000', '-f', 's16le', '-'], capture_output=True, timeout=120)
            samples = np.abs(np.frombuffer(result.stdout, np.int16).astype(np.float32))
        except (OSError, subprocess.SubprocessError):
            samples = np.zeros(0, np.float32)
        if samples.size:
            bins = max(1, samples.size // 20)  # 100 points per second
            usable = samples[:bins * 20].reshape(bins, 20).max(1)
            samples = usable / max(float(usable.max()), 1.0)
        self.ready.emit(samples)


class Waveform(QWidget):
    """Drag across the wave to choose what plays; drag the edges to adjust; click to jump."""
    changed = Signal(float, float)  # start, length
    seek = Signal(float)            # a plain click: listen from here

    def __init__(self, total, start, length, accent, parent=None):
        super().__init__(parent)
        self.total = max(total, 0.1)
        self.start, self.length = start, length
        self.accent = QColor(accent)
        self.peaks = np.zeros(0, np.float32)
        self.cursor = None
        self.drag = None
        self.pressed_at = None
        self.playhead = None
        self.view = (0.0, self.total)  # visible seconds; the mouse wheel zooms
        self.setMinimumHeight(110)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.IBeamCursor)

    def set_peaks(self, peaks):
        self.peaks = peaks
        self.update()

    def set_span(self, start, length):
        self.start, self.length = start, length
        self.update()

    def x_of(self, seconds):
        low, high = self.view
        return (seconds - low) / max(high - low, 1e-6) * self.width()

    def seconds_at(self, x):
        low, high = self.view
        return min(max(low + x / max(self.width(), 1) * (high - low), 0.0), self.total)

    def wheelEvent(self, event):
        low, high = self.view
        anchor = self.seconds_at(event.position().x())
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        span = min(max((high - low) * factor, min(1.0, self.total)), self.total)
        fraction = (anchor - low) / max(high - low, 1e-6)
        low = min(max(anchor - span * fraction, 0.0), self.total - span)
        self.view = (low, low + span)
        self.update()

    def show_span(self, start, length):
        """Zoom so the chosen part fills most of the view."""
        span = min(self.total, max(length * 2, 2.0))
        low = min(max(start + length / 2 - span / 2, 0.0), self.total - span)
        self.view = (low, low + span)
        self.update()

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect())
        painter.fillRect(area, QColor('#0d1015'))
        middle = area.height() / 2
        left, right = self.x_of(self.start), self.x_of(self.start + self.length)
        painter.fillRect(QRectF(left, 0, max(2.0, right - left), area.height()), QColor(self.accent.red(), self.accent.green(), self.accent.blue(), 45))
        if self.peaks.size:
            width = int(area.width())
            low, high = self.view
            # 100 envelope points per second; take the visible stretch.
            first = min(int(low * 100), self.peaks.size - 1)
            last = max(first + 2, min(int(np.ceil(high * 100)), self.peaks.size))
            visible = self.peaks[first:last]
            points = np.interp(np.linspace(0, visible.size - 1, max(width, 2)), np.arange(visible.size), visible)
            for x, value in enumerate(points):
                inside = left <= x <= right
                painter.setPen(QPen(self.accent if inside else QColor('#3d4655'), 1))
                height = max(1.0, value * (middle - 6))
                painter.drawLine(QPointF(x, middle - height), QPointF(x, middle + height))
        else:
            painter.setPen(QColor('#76818f'))
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, 'φόρτωση κύματος…')
        painter.setPen(QPen(self.accent, 2))
        for edge in (left, right):
            painter.drawLine(QPointF(edge, 0), QPointF(edge, area.height()))
        painter.setPen(QColor('#a4aebd'))
        low, high = self.view
        painter.drawText(QRectF(4, 2, 200, 16), Qt.AlignmentFlag.AlignLeft, clock(low))
        zoomed = high - low < self.total - 0.01
        painter.drawText(QRectF(area.width() - 204, 2, 200, 16), Qt.AlignmentFlag.AlignRight, clock(high) + (' · zoom' if zoomed else ''))
        if self.cursor is not None:
            painter.setPen(QPen(QColor(255, 255, 255, 110), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(self.cursor, 0), QPointF(self.cursor, area.height()))
        if self.playhead is not None:
            x = self.x_of(self.playhead)
            painter.setPen(QPen(QColor('#ffffff'), 2))
            painter.drawLine(QPointF(x, 0), QPointF(x, area.height()))
            painter.setBrush(QColor('#ffffff'))
            painter.drawPolygon([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 8)])

    def set_playhead(self, seconds):
        if self.drag and self.drag[0] == 'playhead':
            return  # The user is holding the line.
        self.playhead = seconds
        low, high = self.view
        if seconds is not None and not low <= seconds <= high:
            # Zoomed in and the music moved on: follow it.
            span = high - low
            low = min(max(seconds - span * 0.1, 0.0), self.total - span)
            self.view = (low, low + span)
        self.update()

    def on_playhead(self, x, y):
        """The white line can be grabbed; its triangle on top wins over selection edges."""
        if self.playhead is None or abs(x - self.x_of(self.playhead)) >= 8:
            return False
        left, right = self.x_of(self.start), self.x_of(self.start + self.length)
        return y < 18 or (abs(x - left) >= 8 and abs(x - right) >= 8)

    def mousePressEvent(self, event):
        x, y = event.position().x(), event.position().y()
        self.pressed_at = x
        left, right = self.x_of(self.start), self.x_of(self.start + self.length)
        if self.on_playhead(x, y):
            self.drag = ('playhead', None)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif abs(x - left) < 8:
            self.drag = ('start', None)
        elif abs(x - right) < 8:
            self.drag = ('end', None)
        else:
            self.drag = ('new', self.seconds_at(x))

    def mouseMoveEvent(self, event):
        x = event.position().x()
        self.cursor = x
        left, right = self.x_of(self.start), self.x_of(self.start + self.length)
        near_edge = abs(x - left) < 8 or abs(x - right) < 8
        if self.drag and self.drag[0] == 'playhead':
            # Scrub: the line follows the mouse; listening moves there on release.
            self.playhead = self.seconds_at(x)
            self.update()
            return
        if not self.drag and self.on_playhead(x, event.position().y()):
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor if near_edge or (self.drag and self.drag[0] != 'new') else Qt.CursorShape.IBeamCursor)
        if self.drag and self.drag[0] == 'new' and abs(x - (self.pressed_at or x)) < 4:
            self.update()
            return  # Not a drag yet: may still be a click to jump.
        if self.drag:
            seconds = self.seconds_at(x)
            end = self.start + self.length
            kind, anchor = self.drag
            if kind == 'start':
                start = min(seconds, end - 0.2)
                self.start, self.length = start, end - start
            elif kind == 'end':
                self.length = max(0.2, seconds - self.start)
            else:
                low, high = sorted((anchor, seconds))
                self.start, self.length = low, max(0.2, high - low)
            self.changed.emit(self.start, self.length)
        self.update()

    def mouseReleaseEvent(self, event):
        x = event.position().x()
        if self.drag and self.drag[0] == 'playhead':
            self.drag = None
            self.seek.emit(self.seconds_at(x))
        elif self.drag and self.drag[0] == 'new' and abs(x - (self.pressed_at or x)) < 4:
            self.seek.emit(self.seconds_at(x))
        self.drag = None
        self.pressed_at = None

    def leaveEvent(self, _):
        self.cursor = None
        self.update()


class SoundEditor(QDialog):
    """Pick the seconds on the waveform (or type them), volume, repeat and pad colour."""

    def __init__(self, item, index, player, parent=None):
        super().__init__(parent)
        self.item = replace(item)
        self.player = player
        self.total = length_of(item.path)
        self.accent = colour(item, index)
        self.setWindowTitle('Ήχος')
        self.setMinimumWidth(660)
        layout = QVBoxLayout(self)
        self.name = QLineEdit(item.name)
        self.name.setPlaceholderText('Όνομα pad')
        layout.addWidget(self.name)
        self.wave = Waveform(self.total or item.start + item.length, item.start, item.length, self.accent)
        self.wave.changed.connect(self.wave_changed)
        layout.addWidget(self.wave)
        transport = QHBoxLayout()
        self.play_button = QPushButton('▶ Play')
        self.play_button.setToolTip('Ακούς όλο τον ήχο από τη λευκή γραμμή (Space)')
        self.play_button.clicked.connect(self.play_pause)
        self.selection_button = QPushButton('▶ Επιλογή')
        self.selection_button.setMinimumWidth(130)
        self.selection_button.setObjectName('playselection')
        self.selection_button.setToolTip('Παίζει μόνο το κομμάτι που διάλεξες (με επανάληψη αν είναι τσεκαρισμένη)')
        self.selection_button.clicked.connect(self.play_selection)
        stop = QPushButton('■ Stop')
        stop.clicked.connect(self.stop_listening)
        self.clock = QLabel()
        self.clock.setMinimumWidth(120)
        mark_in = QPushButton('⟦ Αρχή εδώ')
        mark_in.setToolTip('Το κομμάτι ξεκινά από εκεί που ακούς τώρα')
        mark_in.clicked.connect(self.mark_in)
        mark_out = QPushButton('Τέλος εδώ ⟧')
        mark_out.setToolTip('Το κομμάτι τελειώνει εκεί που ακούς τώρα')
        mark_out.clicked.connect(self.mark_out)
        zoom_in = QPushButton('🔍 Zoom στην επιλογή')
        zoom_in.setToolTip('Zoom στο κομμάτι που διάλεξες (ή ροδέλα πάνω στο κύμα)')
        zoom_in.clicked.connect(lambda: self.wave.show_span(self.start.value(), self.length.value()))
        zoom_out = QPushButton('Όλο')
        zoom_out.setToolTip('Δείξε όλο τον ήχο')
        zoom_out.clicked.connect(lambda: (setattr(self.wave, 'view', (0.0, self.wave.total)), self.wave.update()))
        # Row 1: listening. Row 2: view and cue points.
        for widget in (self.selection_button, self.play_button, stop):
            transport.addWidget(widget)
        transport.addSpacing(8)
        transport.addWidget(self.clock)
        transport.addStretch()
        layout.addLayout(transport)
        cues = QHBoxLayout()
        cues.addWidget(zoom_in)
        cues.addWidget(zoom_out)
        cues.addStretch()
        cues.addWidget(mark_in)
        cues.addWidget(mark_out)
        layout.addLayout(cues)
        hint = QLabel('Play για να ακούσεις · σύρε τη λευκή γραμμή ή κάνε κλικ στο κύμα = πήγαινε εκεί · σύρε στο κύμα = διάλεξε κομμάτι · ροδέλα = zoom · «Αρχή/Τέλος εδώ» την ώρα που ακούς')
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        times = QHBoxLayout()
        self.start = QDoubleSpinBox()
        self.length = QDoubleSpinBox()
        for box, label in ((self.start, 'Από'), (self.length, 'για')):
            box.setDecimals(1)
            box.setSingleStep(0.1)
            box.setSuffix(' δευτ.')
            box.setRange(0, self.total or 36000)
            times.addWidget(QLabel(label))
            times.addWidget(box, 1)
        self.length.setMinimum(0.2)
        self.start.setValue(item.start)
        self.length.setValue(item.length)
        form.addRow('Κομμάτι', times)
        volume_row = QHBoxLayout()
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(5, 200)
        self.volume.setValue(item.volume)
        self.volume_label = QLabel()
        volume_row.addWidget(self.volume, 1)
        volume_row.addWidget(self.volume_label)
        form.addRow('Ένταση', volume_row)
        extras = QHBoxLayout()
        self.loop = QCheckBox('🔁 Επανάληψη · παίζει από πίσω μέχρι να το σταματήσεις')
        self.loop.setChecked(item.loop)
        extras.addWidget(self.loop, 1)
        self.colour_button = QPushButton('Χρώμα pad')
        self.colour_button.clicked.connect(self.pick_colour)
        extras.addWidget(self.colour_button)
        form.addRow('', extras)
        layout.addLayout(form)
        self.span = QLabel()
        self.span.setObjectName('muted')
        layout.addWidget(self.span)
        heard = QLabel('Η ακρόαση εδώ παίζει μόνο στα ηχεία σου.')
        heard.setObjectName('muted')
        layout.addWidget(heard)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.start.valueChanged.connect(self.numbers_changed)
        self.length.valueChanged.connect(self.numbers_changed)
        self.volume.valueChanged.connect(self.update_labels)
        self.update_labels()
        self.paint_colour()
        self.peaks = Peaks(item.path, self)
        self.peaks.ready.connect(self.wave.set_peaks)
        self.peaks.start()
        # Listening: where the playhead is, and when playback from there began.
        self.position = item.start
        self.listening_since = None
        self.selection = None  # (start, length, loop) while only the chosen part plays
        self.wave.seek.connect(self.jump)
        self.player.stopped.connect(self.playback_stopped)
        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self.tick)
        self.wave.set_playhead(self.position)
        self.update_clock()
        space = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        space.activated.connect(self.space)

    # -- listening ---------------------------------------------------------------------

    def now(self):
        if self.listening_since is None:
            return self.position
        elapsed = time.monotonic() - self.listening_since
        if self.selection:
            begin, length, loop = self.selection
            return begin + (elapsed % length if loop else min(elapsed, length))
        return min(self.position + elapsed, self.total or 1e9)

    def update_clock(self):
        self.clock.setText(f'{clock(self.now())} / {clock(self.total)}')

    def show_idle(self):
        self.listening_since = None
        self.selection = None
        self.play_button.setText('▶ Play')
        self.selection_button.setText('▶ Επιλογή')
        self.ticker.stop()

    def listen_from(self, seconds):
        """The whole sound from ``seconds`` on."""
        self.player.stop('listen')
        self.selection = None
        self.position = max(0.0, min(seconds, max(0.0, (self.total or seconds) - 0.05)))
        remaining = (self.total - self.position) if self.total else 600
        whole = replace(self.selected(), identifier='listen', start=self.position, length=max(0.05, remaining), loop=False)
        self.player.play(whole, to_mic=False)
        self.listening_since = time.monotonic()
        self.play_button.setText('❚❚ Pause')
        self.selection_button.setText('▶ Επιλογή')
        self.ticker.start(30)

    def play_selection(self):
        """Only the chosen part, exactly as the pad will play it."""
        if self.selection and self.listening_since is not None:
            self.stop_listening()
            return
        item = replace(self.selected(), identifier='listen')
        self.player.stop('listen')
        self.selection = (item.start, item.length, item.loop)
        self.position = item.start
        self.player.play(item, to_mic=False)
        self.listening_since = time.monotonic()
        self.play_button.setText('▶ Play')
        self.selection_button.setText('■ Επιλογή')
        self.ticker.start(30)

    def play_pause(self):
        if self.listening_since is not None and not self.selection:
            self.position = self.now()  # Pause where we are.
            self.player.stop('listen')
            self.show_idle()
        else:
            if self.selection:
                self.position = self.now()
            if self.total and self.position >= self.total - 0.05:
                self.position = 0.0
            self.listen_from(self.position)
        self.wave.set_playhead(self.position)
        self.update_clock()

    def stop_listening(self):
        self.player.stop('listen')
        self.show_idle()
        self.position = self.start.value()
        self.wave.set_playhead(self.position)
        self.update_clock()

    def jump(self, seconds):
        if self.listening_since is not None:
            self.listen_from(seconds)
        else:
            self.position = seconds
        self.wave.set_playhead(self.position)
        self.update_clock()

    def tick(self):
        self.wave.set_playhead(self.now())
        self.update_clock()

    def playback_stopped(self, identifier):
        if identifier != 'listen' or self.listening_since is None:
            return
        # Reached the end: the whole file, or the chosen part (back to its start).
        self.position = self.start.value() if self.selection else self.now()
        self.show_idle()
        self.wave.set_playhead(self.position)
        self.update_clock()

    def mark_in(self):
        here = self.now()
        end = self.start.value() + self.length.value()
        self.start.setValue(round(here, 1))
        if end > here + 0.2:
            self.length.setValue(round(end - here, 1))

    def mark_out(self):
        here = self.now()
        if here - self.start.value() >= 0.2:
            self.length.setValue(round(here - self.start.value(), 1))
        else:
            self.status_hint('Το τέλος πρέπει να είναι μετά την αρχή.')

    def status_hint(self, text):
        self.span.setText(text)

    def space(self):
        # Space is play/pause everywhere in the editor, except while typing the name.
        if self.name.hasFocus():
            self.name.insert(' ')
        else:
            self.play_pause()

    def wave_changed(self, start, length):
        for box, value in ((self.start, start), (self.length, length)):
            box.blockSignals(True)
            box.setValue(round(value, 1))
            box.blockSignals(False)
        self.update_labels()

    def numbers_changed(self):
        if self.total and self.start.value() + self.length.value() > self.total:
            self.length.blockSignals(True)
            self.length.setValue(max(0.2, self.total - self.start.value()))
            self.length.blockSignals(False)
        self.wave.set_span(self.start.value(), self.length.value())
        self.update_labels()

    def update_labels(self):
        end = self.start.value() + self.length.value()
        self.span.setText(f'Παίζει {clock(self.start.value())} → {clock(end)} · {self.length.value():.1f} δευτ.' + (f' από {clock(self.total)}' if self.total else ''))
        self.volume_label.setText(f'{self.volume.value()}%')

    def pick_colour(self):
        chosen = QColorDialog.getColor(self.accent, self, 'Χρώμα pad')
        if chosen.isValid():
            self.accent = chosen
            self.item = replace(self.item, color=chosen.name())
            self.wave.accent = chosen
            self.wave.update()
            self.paint_colour()

    def paint_colour(self):
        self.colour_button.setStyleSheet(f'QPushButton {{ border-left: 14px solid {self.accent.name()}; }}')

    def selected(self):
        return replace(self.item, name=self.name.text().strip() or self.item.name, start=self.start.value(),
            length=self.length.value(), volume=self.volume.value(), loop=self.loop.isChecked())

    def done(self, result):
        self.ticker.stop()
        self.listening_since = None
        self.player.stop('preview')
        self.player.stop('listen')
        try:
            self.player.stopped.disconnect(self.playback_stopped)
        except (RuntimeError, TypeError):
            pass
        if self.peaks.isRunning():
            self.peaks.wait(3000)
        super().done(result)


class Pad(QAbstractButton):
    """A square pad that lights up while its sound plays, with a progress bar."""
    menu_requested = Signal()

    def __init__(self, item, index, player, parent=None):
        super().__init__(parent)
        self.item, self.index, self.player = item, index, player
        self.key = KEYS[index] if index < len(KEYS) else ''
        self.setMinimumSize(120, 96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f'Κλικ{" ή πλήκτρο " + self.key if self.key else ""}: παίζει / σταματά · Δεξί κλικ: επεξεργασία, χρώμα, αφαίρεση')
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda _: self.menu_requested.emit())

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        base = colour(self.item, self.index)
        progress = self.player.progress(self.item.identifier)
        playing = progress is not None
        shape = QPainterPath()
        shape.addRoundedRect(area, 12, 12)
        if playing:
            glow = QColor(base)
            glow.setAlpha(110)
            painter.setPen(QPen(glow, 6))
            painter.drawPath(shape)
        fill = QLinearGradient(area.topLeft(), area.bottomRight())
        light = QColor(base).lighter(125 if playing else 100)
        dark = QColor(base).darker(170 if playing else 320)
        fill.setColorAt(0, light if playing else QColor(base).darker(230))
        fill.setColorAt(1, dark)
        painter.fillPath(shape, fill)
        painter.setPen(QPen(base if not self.isDown() else QColor('#ffffff'), 2))
        painter.drawPath(shape)
        if playing:
            bar = QRectF(area.left() + 10, area.bottom() - 12, (area.width() - 20) * progress, 5)
            painter.fillRect(QRectF(area.left() + 10, area.bottom() - 12, area.width() - 20, 5), QColor(0, 0, 0, 90))
            painter.fillRect(bar, QColor('#ffffff'))
        painter.setPen(QColor('#ffffff') if playing else QColor('#edf0f6'))
        title = QFont(self.font())
        title.setPointSizeF(title.pointSizeF() * 1.15)
        title.setBold(True)
        painter.setFont(title)
        text_area = area.adjusted(12, 22, -12, -20)
        painter.drawText(text_area, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.item.name)
        small = QFont(self.font())
        small.setPointSizeF(small.pointSizeF() * 0.85)
        painter.setFont(small)
        painter.setPen(QColor(255, 255, 255, 170))
        if self.key:
            painter.drawText(area.adjusted(10, 6, -10, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, self.key)
        detail = ('🔁 ' if self.item.loop else '') + f'{self.item.length:.1f}s'
        painter.drawText(area.adjusted(10, 6, -10, 0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, detail)


class LevelBar(QWidget):
    """A simple peak meter: green, amber near the top, red when clipping."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.value = 0.0
        self.hold = 0.0
        self.setMinimumHeight(10)
        self.setMaximumHeight(10)

    def set_level(self, peak):
        self.value = max(peak, self.value * 0.7)  # fast up, smooth fall
        self.hold = max(peak, self.hold - 0.01)
        self.update()

    def paintEvent(self, _):
        painter = QPainter(self)
        area = QRectF(self.rect())
        painter.fillRect(area, QColor('#12151b'))
        # dB scale (-48..0) reads like a mixer meter.
        position = lambda peak: max(0.0, min(1.0, (20 * np.log10(max(peak, 1e-6)) + 48) / 48))
        width = area.width() * position(self.value)
        colour = '#52d273' if self.value < 0.5 else ('#ffd60a' if self.value < 0.9 else '#ff4d6d')
        painter.fillRect(QRectF(0, 0, width, area.height()), QColor(colour))
        painter.fillRect(QRectF(area.width() * position(self.hold) - 2, 0, 2, area.height()), QColor('#edf0f6'))


class AddPad(QAbstractButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(120, 96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip('Νέος ήχος από link ή αρχείο')

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        painter.setPen(QPen(QColor('#61d6c4' if self.underMouse() else '#3d4655'), 2, Qt.PenStyle.DashLine))
        painter.drawRoundedRect(area, 12, 12)
        painter.setPen(QColor('#a4aebd'))
        painter.drawText(area, Qt.AlignmentFlag.AlignCenter, '+\nΝέος ήχος')

    def enterEvent(self, _):
        self.update()

    def leaveEvent(self, _):
        self.update()


class SoundboardPage(QWidget):
    """The whole Soundboard tab."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.player = window.player
        self.download_job = None
        self.pads = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        deck = QHBoxLayout()
        self.air = QPushButton('ON AIR · ανενεργό')
        self.air.setObjectName('onair')
        self.air.setCheckable(True)
        self.air.setMinimumHeight(44)
        self.air.setToolTip('Φτιάχνει το «PhoneCam Mic» (φωνή + ήχοι). Στο Discord: Συσκευή εισόδου → «PhoneCam Mic».')
        self.air.clicked.connect(self.toggle_mic)
        deck.addWidget(self.air, 2)
        master = QVBoxLayout()
        self.master_label = QLabel()
        self.master = QSlider(Qt.Orientation.Horizontal)
        self.master.setRange(0, 150)
        self.master.setValue(int(window.preferences.data.get('sound_master', 100)))
        self.master.valueChanged.connect(self.master_changed)
        master.addWidget(self.master_label)
        master.addWidget(self.master)
        deck.addLayout(master, 2)
        self.stop = QPushButton('■ STOP')
        self.stop.setObjectName('stopall')
        self.stop.setMinimumHeight(44)
        self.stop.setToolTip('Σταματά όλους τους ήχους (Esc)')
        self.stop.clicked.connect(self.player.stop_all)
        deck.addWidget(self.stop, 1)
        layout.addLayout(deck)
        meter_row = QHBoxLayout()
        meter_label = QLabel('Στάθμη PhoneCam Mic')
        meter_label.setObjectName('muted')
        meter_row.addWidget(meter_label)
        self.meter = LevelBar()
        meter_row.addWidget(self.meter, 1)
        layout.addLayout(meter_row)
        self.meter_thread = None
        options = QHBoxLayout()
        self.voice = QCheckBox('Η φωνή μου μαζί')
        self.hear = QCheckBox('Ακούω κι εγώ τους ήχους')
        for box, key in ((self.voice, 'sound_voice'), (self.hear, 'sound_hear')):
            box.setChecked(bool(window.preferences.data.get(key, True)))
            box.toggled.connect(lambda checked, key=key: self.option_changed(key, checked))
            options.addWidget(box)
        options.addStretch()
        layout.addLayout(options)
        self.hint = QLabel('')
        self.hint.setObjectName('muted')
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.grid_host = QWidget()
        self.grid_host.setObjectName('soundgrid')
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(10)
        self.grid.setContentsMargins(10, 10, 10, 10)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.grid_host)
        layout.addWidget(scroll, 1)
        self.status = QLabel('Πλήκτρα 1–0, Q–P, A–L, Z–M παίζουν τα pads · Esc σταματά όλα')
        self.status.setObjectName('muted')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.player.started.connect(lambda _: self.animate())
        self.player.stopped.connect(lambda _: self.animate())
        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self.tick)
        self.master_changed(self.master.value(), save=False)
        self.update_mic()
        self.rebuild()

    # -- deck ----------------------------------------------------------------------

    def master_changed(self, value, save=True):
        self.player.master = value
        self.master_label.setText(f'Master ένταση · {value}%')
        if save:
            self.window.preferences.data['sound_master'] = value
            self.window.preferences.save()

    def update_meter(self):
        running = self.meter_thread is not None
        if self.window.router.active and not running:
            self.meter_thread = sound.Meter(self)
            self.meter_thread.level.connect(self.meter.set_level)
            self.meter_thread.start()
        elif not self.window.router.active and running:
            self.meter_thread.stop()
            self.meter_thread = None
            self.meter.set_level(0)

    def update_mic(self):
        self.update_meter()
        active = self.window.router.active
        self.air.setChecked(active)
        self.air.setText('● ON AIR · «PhoneCam Mic»' if active else 'ON AIR · ανενεργό')
        if not sound.available():
            self.air.setEnabled(False)
            self.hint.setText('Λείπουν τα pactl / paplay. Εγκατάστησε το pulseaudio-utils (Ubuntu/Debian/Fedora) ή libpulse (Arch).')
        elif active:
            self.hint.setText('Discord → Ρυθμίσεις → Φωνή & Βίντεο: Συσκευή εισόδου «PhoneCam Mic» · '
                              'Καταστολή θορύβου: Καμία · Ακύρωση ηχούς: off · Αυτόματος έλεγχος έντασης: off · '
                              'Προηγμένη ανίχνευση φωνής: off · Ευαισθησία εισόδου: όχι αυτόματη, slider χαμηλά. '
                              'Αλλιώς το Discord κρατά μόνο ομιλία και κόβει ή χαμηλώνει τους ήχους. '
                              'Αν η στάθμη εδώ χτυπά ψηλά αλλά δεν σε ακούνε, φταίνε αυτές οι ρυθμίσεις.')
        else:
            self.hint.setText('Πάτα ON AIR για να ακούγονται οι ήχοι στο Discord. Χωρίς αυτό παίζουν μόνο στα ηχεία σου.')

    def toggle_mic(self, checked):
        try:
            if checked:
                voice = self.window.router.start(self.voice.isChecked(), self.hear.isChecked())
                if self.voice.isChecked() and not voice:
                    self.status.setText('Δεν βρέθηκε προεπιλεγμένο μικρόφωνο· ακούγονται μόνο οι ήχοι.')
            else:
                self.player.stop_all()
                self.window.router.stop()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self.status.setText(f'Δεν ενεργοποιήθηκε το μικρόφωνο: {exc}')
        self.window.preferences.data['sound_mic'] = self.window.router.active
        self.window.preferences.save()
        self.update_mic()

    def option_changed(self, key, checked):
        self.window.preferences.data[key] = checked
        self.window.preferences.save()
        if self.window.router.active:
            self.toggle_mic(True)  # Rebuild routing with the new choice.

    # -- pads ----------------------------------------------------------------------

    def sounds(self):
        return self.window.sounds

    def rebuild(self):
        while self.grid.count():
            widget = self.grid.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        self.pads = {}
        for index, item in enumerate(self.sounds()):
            pad = Pad(item, index, self.player)
            pad.clicked.connect(lambda _=False, item=item: self.trigger(item))
            pad.menu_requested.connect(lambda item=item, index=index, pad=pad: self.menu(item, index, pad))
            self.grid.addWidget(pad, index // COLUMNS, index % COLUMNS)
            self.pads[item.identifier] = pad
        add = AddPad()
        add.clicked.connect(lambda: self.add_menu(add))
        count = len(self.sounds())
        self.grid.addWidget(add, count // COLUMNS, count % COLUMNS)
        rows = count // COLUMNS + 1
        for row in range(rows):
            self.grid.setRowStretch(row, 0)
        self.grid.setRowStretch(rows, 1)
        self.animate()

    def animate(self):
        for pad in self.pads.values():
            pad.update()
        if self.player.playing and not self.ticker.isActive():
            self.ticker.start(33)

    def tick(self):
        for identifier, pad in self.pads.items():
            if self.player.is_playing(identifier):
                pad.update()
        if not self.player.playing:
            self.ticker.stop()
            for pad in self.pads.values():
                pad.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.player.stop_all()
            return
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
            return super().keyPressEvent(event)
        index = SCANCODES.get(event.nativeScanCode())
        if index is None and event.text():
            letter = event.text().upper()
            index = KEYS.index(letter) if letter in KEYS else None
        if index is None:
            return super().keyPressEvent(event)
        if not event.isAutoRepeat():
            self.press(index)

    def press(self, index):
        if index < len(self.sounds()):
            self.trigger(self.sounds()[index])

    def trigger(self, item):
        if self.player.is_playing(item.identifier):
            self.player.stop(item.identifier)
        else:
            self.player.play(item, to_mic=self.window.router.active)

    def menu(self, item, index, pad):
        menu = QMenu(self)
        menu.addAction('Επεξεργασία…', lambda: self.edit(item, index))
        menu.addAction('Μετακίνηση αριστερά', lambda: self.move(item, -1))
        menu.addAction('Μετακίνηση δεξιά', lambda: self.move(item, 1))
        menu.addSeparator()
        menu.addAction('Αφαίρεση', lambda: self.remove(item))
        menu.exec(pad.mapToGlobal(pad.rect().center()))

    def add_menu(self, anchor):
        menu = QMenu(self)
        menu.addAction('Από link (YouTube κ.ά.)…', self.ask_link)
        menu.addAction('Από αρχείο…', self.add_file)
        menu.exec(anchor.mapToGlobal(anchor.rect().center()))

    def store(self, sounds):
        self.window.sounds = sounds
        self.window.save_sounds()
        self.rebuild()

    def edit(self, item, index):
        editor = SoundEditor(item, index, self.player, self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            updated = editor.selected()
            self.store([updated if entry.identifier == item.identifier else entry for entry in self.sounds()])

    def move(self, item, step):
        sounds = list(self.sounds())
        index = sounds.index(item)
        target = index + step
        if 0 <= target < len(sounds):
            sounds[index], sounds[target] = sounds[target], sounds[index]
            self.store(sounds)

    def remove(self, item):
        owned = Path(item.path).parent == youtube.SOUNDS
        text = f'Αφαίρεση «{item.name}»;' + ('\n\nΤο κατεβασμένο αρχείο θα σβηστεί.' if owned else f'\n\nΤο αρχείο σου μένει: {item.path}')
        if QMessageBox.question(self, 'Αφαίρεση ήχου', text) != QMessageBox.StandardButton.Yes:
            return
        self.player.stop(item.identifier)
        remaining = [entry for entry in self.sounds() if entry.identifier != item.identifier]
        # Another pad may still use the same download with other seconds.
        if owned and not any(entry.path == item.path for entry in remaining):
            Path(item.path).unlink(missing_ok=True)
        self.store(remaining)

    def add(self, path, name, link=''):
        total = length_of(path)
        item = sound.Sound(name=name[:60], path=path, start=0.0, length=round(min(total or 5.0, 5.0), 1), link=link)
        editor = SoundEditor(item, len(self.sounds()), self.player, self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            self.store(self.sounds() + [editor.selected()])
        elif Path(path).parent == youtube.SOUNDS and not any(entry.path == path for entry in self.sounds()):
            Path(path).unlink(missing_ok=True)  # Cancelled: don't keep the download.

    def add_file(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Ήχος', str(Path.home() / 'Music'), AUDIO)
        if path:
            self.add(path, Path(path).stem)

    def ask_link(self):
        link, accepted = QInputDialog.getText(self, 'Ήχος από link', 'Επικόλλησε link (YouTube ή άλλο site):')
        link = link.strip()
        if not accepted or not link:
            return
        if not youtube.looks_like_link(link):
            self.status.setText('Αυτό δεν μοιάζει με link.')
            return
        job = Download(link, self)
        job.progress.connect(self.status.setText)
        job.failed.connect(self.status.setText)
        job.done.connect(lambda path, title, link: (self.status.setText('Κατέβηκε· διάλεξε στο κύμα ποια δευτερόλεπτα θα παίζει.'), self.add(path, title, link)))
        job.finished.connect(job.deleteLater)
        self.download_job = job
        self.status.setText('Λήψη ήχου…')
        job.start()
