"""Soundboard window: sounds from links or files, trimmed to the seconds you choose."""
from dataclasses import replace
from pathlib import Path
import subprocess
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSlider, QVBoxLayout, QWidget)
from . import scene, sound, youtube

AUDIO = 'Ήχοι και βίντεο (*.mp3 *.ogg *.opus *.wav *.flac *.m4a *.aac *.webm *.mp4 *.mkv);;Όλα (*)'


def clock(seconds):
    minutes, seconds = divmod(max(0.0, seconds), 60)
    return f'{int(minutes)}:{seconds:04.1f}'


def length_of(path):
    try:
        return scene.duration(path)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


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


class SoundEditor(QDialog):
    """Name, which seconds play, volume and repeat; with a preview on your own speakers."""

    def __init__(self, item, player, parent=None):
        super().__init__(parent)
        self.item = replace(item)
        self.player = player
        self.total = length_of(item.path)
        self.setWindowTitle('Ήχος')
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(item.name)
        form.addRow('Όνομα', self.name)
        self.start = QDoubleSpinBox()
        self.start.setDecimals(1)
        self.start.setSingleStep(0.5)
        self.start.setSuffix(' δευτ.')
        self.start.setRange(0, max(0.0, self.total - 0.1) if self.total else 36000)
        self.start.setValue(item.start)
        form.addRow('Ξεκινά από', self.start)
        self.length = QDoubleSpinBox()
        self.length.setDecimals(1)
        self.length.setSingleStep(0.5)
        self.length.setSuffix(' δευτ.')
        self.length.setRange(0.2, self.total or 36000)
        self.length.setValue(item.length)
        form.addRow('Παίζει για', self.length)
        self.span = QLabel()
        self.span.setObjectName('muted')
        form.addRow('', self.span)
        volume_row = QHBoxLayout()
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(5, 200)
        self.volume.setValue(item.volume)
        self.volume_label = QLabel()
        volume_row.addWidget(self.volume, 1)
        volume_row.addWidget(self.volume_label)
        form.addRow('Ένταση', volume_row)
        self.loop = QCheckBox('Επανάληψη · παίζει από πίσω μέχρι να το σταματήσεις')
        self.loop.setChecked(item.loop)
        form.addRow('', self.loop)
        layout.addLayout(form)
        preview = QPushButton('▶ Δοκιμή (μόνο στα ηχεία σου)')
        preview.clicked.connect(self.preview)
        layout.addWidget(preview)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for widget in (self.start, self.length):
            widget.valueChanged.connect(self.update_span)
        self.volume.valueChanged.connect(self.update_span)
        self.update_span()

    def update_span(self):
        if self.total:
            self.length.setMaximum(max(0.2, self.total - self.start.value()))
        end = self.start.value() + self.length.value()
        whole = f' από {clock(self.total)}' if self.total else ''
        self.span.setText(f'Παίζει {clock(self.start.value())} → {clock(end)}{whole}')
        self.volume_label.setText(f'{self.volume.value()}%')

    def selected(self):
        return replace(self.item, name=self.name.text().strip() or self.item.name, start=self.start.value(),
            length=self.length.value(), volume=self.volume.value(), loop=self.loop.isChecked())

    def preview(self):
        sample = replace(self.selected(), identifier='preview', loop=False)
        self.player.play(sample, to_mic=False)

    def done(self, result):
        self.player.stop('preview')
        super().done(result)


class Soundboard(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.player = window.player
        self.download_job = None
        self.setWindowTitle('Soundboard')
        self.setMinimumSize(560, 460)
        layout = QVBoxLayout(self)
        mic_row = QHBoxLayout()
        self.mic = QPushButton('Μικρόφωνο για Discord · ανενεργό')
        self.mic.setCheckable(True)
        self.mic.setToolTip('Φτιάχνει το «PhoneCam Mic»: η φωνή σου + οι ήχοι. Διάλεξέ το ως είσοδο στο Discord.')
        self.mic.clicked.connect(self.toggle_mic)
        mic_row.addWidget(self.mic, 1)
        layout.addLayout(mic_row)
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
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.grid.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.grid_host)
        layout.addWidget(scroll, 1)
        self.status = QLabel('')
        self.status.setObjectName('muted')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        add_link = QPushButton('+ Από link…')
        add_link.clicked.connect(self.ask_link)
        actions.addWidget(add_link)
        add_file = QPushButton('+ Από αρχείο…')
        add_file.clicked.connect(self.add_file)
        actions.addWidget(add_file)
        actions.addStretch()
        stop = QPushButton('■ Σταμάτα όλα')
        stop.clicked.connect(self.player.stop_all)
        actions.addWidget(stop)
        layout.addLayout(actions)
        self.player.started.connect(lambda _: self.refresh_buttons())
        self.player.stopped.connect(lambda _: self.refresh_buttons())
        self.update_mic()
        self.rebuild()

    # -- virtual microphone ----------------------------------------------------

    def update_mic(self):
        active = self.window.router.active
        self.mic.setChecked(active)
        self.mic.setText('Μικρόφωνο για Discord · ενεργό' if active else 'Μικρόφωνο για Discord · ανενεργό')
        if not sound.available():
            self.mic.setEnabled(False)
            self.hint.setText('Λείπουν τα pactl / paplay. Εγκατάστησε το pulseaudio-utils (Ubuntu/Debian/Fedora) ή libpulse (Arch).')
        elif active:
            self.hint.setText('Στο Discord → Ρυθμίσεις → Φωνή & Βίντεο → Συσκευή εισόδου: «PhoneCam Mic». '
                              'Για μουσική, κλείσε την καταστολή θορύβου του Discord, αλλιώς κόβει τους ήχους.')
        else:
            self.hint.setText('Ενεργοποίησε το μικρόφωνο για να ακούγονται οι ήχοι στο Discord. Χωρίς αυτό παίζουν μόνο στα ηχεία σου.')

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

    # -- sounds ------------------------------------------------------------------

    def sounds(self):
        return self.window.sounds

    def save(self):
        self.window.save_sounds()

    def rebuild(self):
        while self.grid.count():
            widget = self.grid.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        self.buttons = {}
        if not self.sounds():
            empty = QLabel('Κανένας ήχος ακόμα. Πρόσθεσε από link (YouTube κ.ά.) ή από αρχείο.')
            empty.setObjectName('muted')
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.grid.addWidget(empty, 0, 0)
        for index, item in enumerate(self.sounds()):
            button = QPushButton()
            button.setObjectName('sound')
            button.setMinimumHeight(64)
            button.setCheckable(True)
            button.setToolTip('Κλικ: παίζει / σταματά · Δεξί κλικ: επεξεργασία, αφαίρεση')
            button.clicked.connect(lambda _, item=item: self.trigger(item))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(lambda _, item=item, button=button: self.menu(item, button))
            self.grid.addWidget(button, index // 3, index % 3)
            self.buttons[item.identifier] = (button, item)
        self.refresh_buttons()

    def refresh_buttons(self):
        for identifier, (button, item) in getattr(self, 'buttons', {}).items():
            playing = self.player.is_playing(identifier)
            mark = '■ ' if playing else ('🔁 ' if item.loop else '▶ ')
            button.setText(f'{mark}{item.name}\n{item.length:.1f} δευτ.')
            button.setChecked(playing)

    def trigger(self, item):
        if self.player.is_playing(item.identifier):
            self.player.stop(item.identifier)
        else:
            self.player.play(item, to_mic=self.window.router.active)

    def menu(self, item, button):
        menu = QMenu(self)
        menu.addAction('Επεξεργασία…', lambda: self.edit(item))
        menu.addAction('Αφαίρεση', lambda: self.remove(item))
        menu.exec(button.mapToGlobal(button.rect().center()))

    def edit(self, item):
        editor = SoundEditor(item, self.player, self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            updated = editor.selected()
            self.window.sounds = [updated if entry.identifier == item.identifier else entry for entry in self.sounds()]
            self.save()
            self.rebuild()

    def remove(self, item):
        owned = Path(item.path).parent == youtube.SOUNDS
        text = f'Αφαίρεση «{item.name}»;' + ('\n\nΤο κατεβασμένο αρχείο θα σβηστεί.' if owned else f'\n\nΤο αρχείο σου μένει: {item.path}')
        if QMessageBox.question(self, 'Αφαίρεση ήχου', text) != QMessageBox.StandardButton.Yes:
            return
        self.player.stop(item.identifier)
        self.window.sounds = [entry for entry in self.sounds() if entry.identifier != item.identifier]
        # Another sound may still use the same downloaded file with other seconds.
        if owned and not any(entry.path == item.path for entry in self.sounds()):
            Path(item.path).unlink(missing_ok=True)
        self.save()
        self.rebuild()

    def add(self, path, name, link=''):
        total = length_of(path)
        item = sound.Sound(name=name[:60], path=path, start=0.0, length=round(min(total or 5.0, 5.0), 1), link=link)
        editor = SoundEditor(item, self.player, self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            self.window.sounds = self.sounds() + [editor.selected()]
            self.save()
            self.rebuild()
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
        job.done.connect(lambda path, title, link: (self.status.setText('Κατέβηκε· διάλεξε ποια δευτερόλεπτα θα παίζει.'), self.add(path, title, link)))
        job.finished.connect(job.deleteLater)
        self.download_job = job
        self.status.setText('Λήψη ήχου…')
        job.start()

    def done(self, result):
        if self.download_job and self.download_job.isRunning():
            self.download_job.wait(100)
        super().done(result)
