"""Background library: add images, videos and links; remove them and what PhoneCam made for them."""
from pathlib import Path
import re
import subprocess
from PySide6.QtCore import QSize, QThread, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap, QColor
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout)
from . import scene, youtube

THUMB = QSize(160, 90)
FILTER = ('Εικόνες και βίντεο (*.png *.jpg *.jpeg *.webp *.bmp *.mp4 *.mkv *.webm *.mov *.m4v *.avi *.gif);;'
          'Εικόνες (*.png *.jpg *.jpeg *.webp *.bmp);;Βίντεο (*.mp4 *.mkv *.webm *.mov *.m4v *.avi *.gif)')


def downloaded(path):
    return Path(path).parent == youtube.FOLDER


TITLES = {}  # path -> real title of downloaded videos; the window keeps it in preferences


def title(path):
    if TITLES.get(path):
        return TITLES[path]
    name = Path(path).stem
    if downloaded(path):
        name = re.sub(r' ?\[[\w-]{6,}\]$', '', name).replace('_', ' ').strip()
        return name or f'Βίντεο {youtube.video_id(path)}'
    return name


def kind(path):
    if downloaded(path):
        return 'YouTube'
    return 'Βίντεο' if scene.is_video(path) else 'Εικόνα'


def size_text(size):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.0f} {unit}' if unit in ('B', 'KB') else f'{size:.1f} {unit}'
        size /= 1024


def file_size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


class Thumbnails(QThread):
    """Video thumbnails come from FFmpeg; done off the UI thread and cached."""
    ready = Signal(str, QImage)
    titled = Signal(str, str)

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.paths = paths

    def run(self):
        folder = scene.CACHE / 'thumbs'
        folder.mkdir(parents=True, exist_ok=True)
        for path in self.paths:
            if self.isInterruptionRequested():
                return
            if scene.is_video(path):
                target = folder / f'{scene.source_tag(path)}.jpg'
                if not target.exists():
                    subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-ss', '1', '-i', path,
                        '-frames:v', '1', '-vf', f'scale={THUMB.width() * 2}:-2', str(target)], capture_output=True, timeout=30)
                image = QImage(str(target))
            else:
                image = QImage(path)
            if not image.isNull():
                self.ready.emit(path, image.scaled(THUMB * 2, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation))
            identifier = youtube.video_id(path)
            if downloaded(path) and not TITLES.get(path) and len(identifier) == 11:
                # Older downloads lost non-Latin titles in their file names; ask YouTube once.
                try:
                    name = youtube.lookup_title(f'https://www.youtube.com/watch?v={identifier}')
                except (OSError, subprocess.SubprocessError):
                    name = ''
                if name:
                    self.titled.emit(path, name)


def framed(image, badge):
    """Thumbnail cropped to 16:9, with a small play mark for videos."""
    canvas = QPixmap(THUMB)
    canvas.fill(QColor('#0a0c10'))
    painter = QPainter(canvas)
    if image is not None:
        scaled = image.scaled(THUMB, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
        painter.drawImage((THUMB.width() - scaled.width()) // 2, (THUMB.height() - scaled.height()) // 2, scaled)
    if badge:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(6, THUMB.height() - 24, 22 + 7 * len(badge), 18, 4, 4)
        painter.setPen(QColor('#edf0f6'))
        painter.drawText(12, THUMB.height() - 11, badge)
    painter.end()
    return QIcon(canvas)


class LibraryDialog(QDialog):
    """Acts through the main window, which owns preferences, downloads and the camera."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.thumbs = None
        self.images = {}
        self.setWindowTitle('Φόντα')
        self.setMinimumSize(620, 520)
        layout = QVBoxLayout(self)
        hint = QLabel('Διπλό κλικ για χρήση. Η αφαίρεση σβήνει ό,τι έφτιαξε το PhoneCam (loops, κατεβασμένα βίντεο)· τα δικά σου αρχεία μένουν ανέγγιχτα.')
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.list = QListWidget()
        self.list.setIconSize(THUMB)
        self.list.setSpacing(4)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setUniformItemSizes(True)
        self.list.itemDoubleClicked.connect(lambda _: self.use_selected())
        self.list.currentItemChanged.connect(lambda *_: self.update_buttons())
        layout.addWidget(self.list, 1)
        self.progress = QLabel('')
        self.progress.setObjectName('muted')
        self.progress.setWordWrap(True)
        layout.addWidget(self.progress)
        actions = QHBoxLayout()
        add_file = QPushButton('+ Εικόνα ή βίντεο…')
        add_file.clicked.connect(self.add_file)
        actions.addWidget(add_file)
        add_link = QPushButton('+ YouTube link…')
        add_link.clicked.connect(self.ask_link)
        actions.addWidget(add_link)
        actions.addStretch()
        self.remove_button = QPushButton('Αφαίρεση')
        self.remove_button.clicked.connect(self.remove_selected)
        actions.addWidget(self.remove_button)
        layout.addLayout(actions)
        footer = QHBoxLayout()
        self.space = QLabel('')
        self.space.setObjectName('muted')
        footer.addWidget(self.space, 1)
        self.clean_button = QPushButton('Καθαρισμός cache')
        self.clean_button.setToolTip('Σβήνει τα έτοιμα loops· ξαναφτιάχνονται όταν χρειαστούν.')
        self.clean_button.clicked.connect(self.clean_cache)
        footer.addWidget(self.clean_button)
        self.use_button = QPushButton('Χρήση')
        self.use_button.setObjectName('connect')
        self.use_button.clicked.connect(self.use_selected)
        footer.addWidget(self.use_button)
        close = QPushButton('Κλείσιμο')
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.refresh()

    # -- content -----------------------------------------------------------------

    def entries(self):
        return [path for path in self.window.preferences.data.get('backgrounds', []) if Path(path).is_file()]

    def refresh(self, select=None):
        select = select if select is not None else (self.list.currentItem().data(Qt.ItemDataRole.UserRole) if self.list.currentItem() else self.window.capture.background)
        self.list.clear()
        none = QListWidgetItem(framed(None, ''), 'Κανένα\nπραγματικό δωμάτιο')
        none.setData(Qt.ItemDataRole.UserRole, '')
        self.list.addItem(none)
        active = self.window.capture.background
        for path in self.entries():
            loops = sum(file_size(item) for item in scene.derived_files(path) if item.suffix == '.mkv')
            details = [kind(path), size_text(file_size(path)) if downloaded(path) else Path(path).parent.name]
            if loops:
                details.append(f'έτοιμο loop {size_text(loops)}')
            if path == active:
                details.insert(0, '● σε χρήση')
            badge = '▶ YT' if downloaded(path) else ('▶' if scene.is_video(path) else '')
            item = QListWidgetItem(framed(self.images.get(path), badge), f'{title(path)}\n' + ' · '.join(details))
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            self.list.addItem(item)
        for row in range(self.list.count()):
            if self.list.item(row).data(Qt.ItemDataRole.UserRole) == select:
                self.list.setCurrentRow(row)
                self.list.scrollToItem(self.list.item(row))
                break
        else:
            self.list.setCurrentRow(0)
        total = sum(file_size(item) for item in scene.CACHE.glob('*.mkv')) + sum(file_size(item) for item in youtube.FOLDER.glob('*'))
        self.space.setText(f'Χώρος PhoneCam: {size_text(total)}')
        self.update_buttons()
        missing = [path for path in self.entries() if path not in self.images]
        if missing and not (self.thumbs and self.thumbs.isRunning()):
            self.thumbs = Thumbnails(missing, self)
            self.thumbs.ready.connect(self.thumbnail_ready)
            self.thumbs.titled.connect(self.title_found)
            self.thumbs.start()

    def thumbnail_ready(self, path, image):
        self.images[path] = image
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == path:
                badge = '▶ YT' if downloaded(path) else ('▶' if scene.is_video(path) else '')
                item.setIcon(framed(image, badge))

    def title_found(self, path, name):
        self.window.remember_title(path, name)
        self.refresh()

    def selected(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def update_buttons(self):
        path = self.selected()
        self.remove_button.setEnabled(bool(path))
        self.use_button.setEnabled(path is not None and path != self.window.capture.background)

    # -- actions -----------------------------------------------------------------

    def add_file(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Εικόνα ή βίντεο φόντου', str(Path.home() / 'Videos'), FILTER)
        if path:
            self.window.remember_background(path)
            self.refresh(select=path)

    def ask_link(self):
        link, accepted = QInputDialog.getText(self, 'YouTube φόντο', 'Επικόλλησε το link του βίντεο (YouTube ή άλλο site):')
        link = link.strip()
        if not accepted or not link:
            return
        if not youtube.looks_like_link(link):
            self.progress.setText('Αυτό δεν μοιάζει με link βίντεο.')
            return
        self.progress.setText('Λήψη βίντεο…')
        self.window.download_link(link)

    def use_selected(self):
        path = self.selected()
        if path is not None:
            self.window.use_background(path)
            self.refresh(select=path)

    def remove_selected(self):
        path = self.selected()
        if not path:
            return
        derived = scene.derived_files(path)
        deleting = derived + ([Path(path)] if downloaded(path) else [])
        freed = sum(file_size(item) for item in deleting)
        text = f'Αφαίρεση «{title(path)}» από τη λίστα;'
        if downloaded(path):
            text += f'\n\nΤο κατεβασμένο βίντεο και τα loops του θα σβηστούν ({size_text(freed)}).'
        elif freed:
            text += f'\n\nΘα σβηστούν τα loops που έφτιαξε το PhoneCam ({size_text(freed)}). Το αρχείο σου μένει: {path}'
        else:
            text += f'\n\nΤο αρχείο σου μένει: {path}'
        if path == self.window.capture.background:
            text += '\n\nΕίναι το τρέχον φόντο· η κάμερα θα γυρίσει στο πραγματικό δωμάτιο.'
        if QMessageBox.question(self, 'Αφαίρεση φόντου', text) != QMessageBox.StandardButton.Yes:
            return
        self.window.forget_background(path)
        for item in deleting:
            item.unlink(missing_ok=True)
        self.images.pop(path, None)
        self.refresh(select='')

    def clean_cache(self):
        active = self.window.capture.background
        keep = set(scene.derived_files(active)) if active and Path(active).exists() else set()
        files = [item for item in scene.CACHE.glob('*.mkv') if item not in keep]
        freed = sum(file_size(item) for item in files)
        if not files:
            self.progress.setText('Δεν υπάρχει cache για σβήσιμο.')
            return
        if QMessageBox.question(self, 'Καθαρισμός cache', f'Να σβηστούν {len(files)} έτοιμα loops ({size_text(freed)});\nΤο τρέχον φόντο μένει. Τα υπόλοιπα ξαναφτιάχνονται όταν τα διαλέξεις.') != QMessageBox.StandardButton.Yes:
            return
        for item in files:
            item.unlink(missing_ok=True)
        self.progress.setText(f'Ελευθερώθηκαν {size_text(freed)}.')
        self.refresh()

    def closeEvent(self, event):
        if self.thumbs:
            self.thumbs.requestInterruption()
            self.thumbs.wait(2000)
        super().closeEvent(event)

    def done(self, result):
        if self.thumbs:
            self.thumbs.requestInterruption()
            self.thumbs.wait(2000)
        super().done(result)
