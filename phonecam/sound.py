"""Soundboard audio: a virtual microphone that mixes your voice with the sounds you play.

PulseAudio/PipeWire (through pactl):
    sounds ──► [PhoneCam Sounds] ──┬──► [mix] ──► «PhoneCam Mic» (pick it in Discord)
    your mic ──────────────────────┘   └──► your speakers (optional, so you hear them too)
"""
from dataclasses import asdict, dataclass, field
import re
import shutil
import subprocess
import time
import uuid
import numpy as np
from PySide6.QtCore import QObject, QProcess, QThread, Signal

SOUNDS_SINK = 'phonecam_sounds'
MIX_SINK = 'phonecam_mix'
MIC = 'phonecam_mic'


@dataclass
class Sound:
    name: str
    path: str
    start: float = 0.0
    length: float = 5.0
    volume: int = 100        # percent
    loop: bool = False       # keeps playing behind your voice until stopped
    link: str = ''
    color: str = ''          # pad colour; empty = from the palette by position
    identifier: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        known = {key: value for key, value in data.items() if key in cls.__dataclass_fields__}
        return cls(**known)


def pactl(*arguments):
    return subprocess.run(['pactl', *arguments], capture_output=True, text=True, timeout=10)


def available():
    """pactl and paplay come together (pulseaudio-utils / libpulse) and work on PipeWire too."""
    return bool(shutil.which('pactl') and shutil.which('paplay')) and pactl('info').returncode == 0


def ours():
    """Module ids PhoneCam loaded (also ones left behind by a crash)."""
    result = pactl('list', 'short', 'modules')
    ids = []
    for line in result.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) >= 3 and 'phonecam_' in parts[2]:
            ids.append(parts[0])
    return ids


def default_source():
    result = pactl('get-default-source')
    name = result.stdout.strip()
    # Never loop our own microphone back into itself.
    return '' if not name or name.startswith('phonecam_') else name


class Router:
    """Creates and removes the virtual microphone."""

    def __init__(self):
        self.active = False

    def start(self, with_voice=True, hear_myself=True):
        self.stop()
        voice = default_source() if with_voice else ''
        loads = [
            ['module-null-sink', f'sink_name={SOUNDS_SINK}', 'sink_properties=device.description="PhoneCam Sounds"'],
            ['module-null-sink', f'sink_name={MIX_SINK}', 'sink_properties=device.description="PhoneCam Mix"'],
            ['module-loopback', f'source={SOUNDS_SINK}.monitor', f'sink={MIX_SINK}', 'latency_msec=20', 'source_dont_move=true', 'sink_dont_move=true'],
        ]
        if voice:
            loads.append(['module-loopback', f'source={voice}', f'sink={MIX_SINK}', 'latency_msec=20', 'sink_dont_move=true'])
        if hear_myself:
            loads.append(['module-loopback', f'source={SOUNDS_SINK}.monitor', 'latency_msec=40', 'source_dont_move=true'])
        loads.append(['module-remap-source', f'master={MIX_SINK}.monitor', f'source_name={MIC}', 'source_properties=device.description="PhoneCam Mic"'])
        for module in loads:
            result = pactl('load-module', *module)
            if result.returncode != 0:
                self.stop()
                raise RuntimeError(result.stderr.strip() or 'δεν φορτώθηκε ' + module[0])
        self.active = True
        return voice

    def stop(self):
        for module in reversed(ours()):
            pactl('unload-module', module)
        self.active = False


class Meter(QThread):
    """Level of what really leaves «PhoneCam Mic» (so you can tell PhoneCam from Discord)."""
    level = Signal(float)  # peak, 0..1, about 20 times a second

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None

    def run(self):
        try:
            self.process = subprocess.Popen(['parec', f'--device={MIC}', '--format=s16le', '--channels=1', '--rate=8000',
                '--latency-msec=50', '--client-name=PhoneCam meter'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return
        block = 400 * 2  # 50 ms of 8 kHz mono
        while not self.isInterruptionRequested():
            data = self.process.stdout.read(block)
            if not data:
                break
            samples = np.frombuffer(data[:len(data) // 2 * 2], np.int16)
            self.level.emit(float(np.abs(samples).max()) / 32768 if samples.size else 0.0)

    def stop(self):
        self.requestInterruption()
        if self.process:
            self.process.kill()
        self.wait(1000)


class Player(QObject):
    """Plays trimmed sounds through FFmpeg; several may play at once."""
    started = Signal(str)
    stopped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.playing = {}
        self.timing = {}   # identifier -> (started, length, loop) for progress on the pads
        self.master = 100  # percent, applied when a sound starts

    def progress(self, identifier):
        """0..1 through the clip (loops wrap around), or None when not playing."""
        if identifier not in self.timing:
            return None
        started, length, loop = self.timing[identifier]
        elapsed = (time.monotonic() - started) / max(length, 0.05)
        return elapsed % 1 if loop else min(elapsed, 1.0)

    def play(self, sound, to_mic=True):
        self.stop(sound.identifier)
        # FFmpeg decodes and trims; paplay plays. FFmpeg's own Pulse output ignores the
        # chosen sink under PipeWire, paplay honours it.
        decoder, output = QProcess(self), QProcess(self)
        arguments = ['-hide_banner', '-loglevel', 'error', '-nostdin']
        if sound.start > 0:
            arguments += ['-ss', f'{sound.start:.3f}']
        # Input options: read only the chosen part of the file.
        arguments += ['-t', f'{sound.length:.3f}', '-i', sound.path, '-vn']
        filters = [f'volume={sound.volume * self.master / 10000:.3f}', 'aresample=48000']
        if sound.loop:
            # Hold that part in memory and repeat it until stopped.
            filters.append(f'aloop=loop=-1:size={max(1, int(sound.length * 48000))}')
        arguments += ['-af', ','.join(filters), '-ac', '2', '-f', 's16le', '-']
        # Short buffer: paplay's default (~170 ms here) made every pad start late.
        playback = ['--raw', '--format=s16le', '--rate=48000', '--channels=2', '--client-name=PhoneCam', '--latency-msec=30',
                    '--stream-name=' + re.sub(r'\s+', ' ', sound.name)[:40]]
        if to_mic:
            playback.append(f'--device={SOUNDS_SINK}')
        decoder.setStandardOutputProcess(output)
        output.finished.connect(lambda *_: self.finished(sound.identifier, decoder, output))
        self.playing[sound.identifier] = (decoder, output)
        self.timing[sound.identifier] = (time.monotonic(), sound.length, sound.loop)
        output.start('paplay', playback)
        decoder.start('ffmpeg', arguments)
        self.started.emit(sound.identifier)

    def finished(self, identifier, decoder, output):
        if self.playing.get(identifier) == (decoder, output):
            del self.playing[identifier]
            self.timing.pop(identifier, None)
            self.stopped.emit(identifier)
        decoder.deleteLater()
        output.deleteLater()

    def stop(self, identifier):
        pair = self.playing.pop(identifier, None)
        self.timing.pop(identifier, None)
        if pair:
            for process in pair:
                process.blockSignals(True)
                process.kill()
                process.waitForFinished(500)
                process.deleteLater()
            self.stopped.emit(identifier)

    def stop_all(self):
        for identifier in list(self.playing):
            self.stop(identifier)

    def is_playing(self, identifier):
        return identifier in self.playing
