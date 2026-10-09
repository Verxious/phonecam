from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import time
from PySide6.QtCore import QObject, QProcess, QTimer, Signal
from PySide6.QtGui import QImage
from .config import STATE
from .live import LiveControls, commands, fit_width
from . import scene

FRAME_WIDTH, FRAME_HEIGHT = 960, 540
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * 3


class CaptureEngine(QObject):
    frame = Signal(QImage)
    state = Signal(str)
    failure = Signal(str)
    restored = Signal(object)
    working = Signal(object)
    measured_fps = Signal(float)
    control_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source = None
        self.preview = None
        self.capture = None
        self.good = None
        self.pending = None
        self.device = ''
        self.want_capture = False
        self.stopping = False
        self.closing = False
        self.generation = 0
        self.controls = None
        self.control_endpoint = ''
        self.backdrop = ''
        self.fit = 'auto'
        self.buffer = bytearray()
        self.logs = ''
        self.pipe = STATE / 'preview.mkv.pipe'
        self.received = False
        self.count = 0
        self.count_since = time.monotonic()
        self.first_frame_timer = QTimer(self)
        self.first_frame_timer.setSingleShot(True)
        self.first_frame_timer.timeout.connect(self.start_timeout)

    def start(self, capture, device):
        capture.validated()
        self.generation += 1
        if self.capture and self.received and self.controls and not self.stopping:
            old = replace(self.capture, rotation=capture.rotation, mirror=capture.mirror, look=capture.look, exposure=capture.exposure,
                background=capture.background, motion=capture.motion, background_mirror=capture.background_mirror, fit=capture.fit)
            backdrop = self.backdrop_path(capture)
            # Switching between backgrounds happens inside the running compositor; only
            # turning the background on or off needs a different pipeline.
            if old == capture and bool(backdrop) == bool(self.backdrop):
                self.capture = replace(capture)
                self.controls.submit(self.generation, replace(capture), commands(capture))
                if self.backdrop and self.preview:
                    if (backdrop, capture.fit) != (self.backdrop, self.fit):
                        self.preview.write(('background ' + json.dumps([backdrop, capture.fit]) + '\n').encode())
                        self.backdrop, self.fit = backdrop, capture.fit
                    self.preview.write(f'mirror {int(capture.background_mirror)}\n'.encode())
                return
        if self.good and (capture.source, capture.serial, capture.url) != (self.good.source, self.good.serial, self.good.url):
            self.good = None
        self.device = device
        self.want_capture = True
        if self.source and self.source.state() != QProcess.ProcessState.NotRunning:
            self.pending = replace(capture)
            self.stopping = True
            self.first_frame_timer.stop()
            self.source.terminate()
            process = self.source
            QTimer.singleShot(1500, lambda: process.kill() if self.source is process and process.state() != QProcess.ProcessState.NotRunning else None)
            self.stop_preview()
            self.state.emit('Εφαρμογή ρυθμίσεων…')
            return
        self.begin(replace(capture))

    def begin(self, capture):
        if not self.want_capture or self.closing:
            return
        self.capture = capture
        self.pending = None
        self.stopping = False
        self.received = False
        self.logs = ''
        self.buffer.clear()
        self.count = 0
        self.count_since = time.monotonic()
        if self.pipe.exists() and not self.pipe.is_fifo():
            self.want_capture = False
            self.failure.emit('Το αρχείο προεπισκόπησης δεν είναι διαθέσιμο.')
            return
        if not self.pipe.exists():
            os.mkfifo(self.pipe, 0o600)
        # Configure the advertised rate; this does not invent source frames.
        try:
            subprocess.run(['v4l2loopback-ctl', 'set-fps', self.device, str(capture.fps)], capture_output=True, timeout=2, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.start_preview()
        self.source = QProcess(self)
        self.source.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.source.readyReadStandardOutput.connect(self.read_logs)
        self.source.finished.connect(self.source_finished)
        self.source.errorOccurred.connect(self.source_error)
        if capture.source == 'android':
            program = 'scrcpy'
            arguments = [
                '--serial', capture.serial, '--video-source=camera', '--camera-id=' + capture.camera,
                '--camera-size=' + capture.size, '--camera-fps=' + str(capture.fps),
                '--capture-orientation=0',
                '--video-codec=h264', '--video-bit-rate=' + str(capture.bitrate) + 'M',
                '--no-downsize-on-error', '--no-audio', '--no-control', '--no-video-playback', '--no-window',
                '--record-format=mkv', '--record=' + str(self.pipe),
            ]
        else:
            program = 'ffmpeg'
            arguments = self.network_arguments(capture)
        self.state.emit('Σύνδεση κάμερας…')
        self.source.start(program, arguments)
        self.first_frame_timer.start(18000)

    def network_arguments(self, capture):
        arguments = ['-y', '-hide_banner', '-loglevel', 'warning', '-rw_timeout', '10000000']
        if capture.url.startswith(('rtsp://', 'rtsps://')):
            arguments += ['-rtsp_transport', 'tcp']
        return arguments + ['-i', capture.url, '-map', '0:v:0', '-an', '-c:v', 'copy', '-flush_packets', '1', '-f', 'matroska', str(self.pipe)]

    def image_filters(self, capture):
        filters = []
        if capture.source == 'network':
            filters = ['scale=' + capture.size.replace('x', ':'), 'fps=' + str(capture.fps)]
        brightness = capture.exposure / 100
        contrast = 1.08 if capture.look == 'vivid' else 1
        saturation = {'natural': 1, 'warm': 1.05, 'mono': 0, 'vivid': 1.35}[capture.look]
        filters.append(f'eq@tone=brightness={brightness}:contrast={contrast}:saturation={saturation}')
        filters.append(f'colorbalance@warmth=rs={0.08 if capture.look == "warm" else 0}:bs={-0.06 if capture.look == "warm" else 0}')
        width, height = capture.size.split('x')
        filters.extend([
            f'scale@fit=w={fit_width(capture)}:h=-2:eval=frame',
            f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:eval=frame',
            f'rotate@turn=angle={capture.rotation}*PI/180:ow=iw:oh=ih:bilinear=0',
            f'hflip@mirror=enable={int(capture.mirror)}',
        ])
        return ','.join(filters)

    @staticmethod
    def backdrop_path(capture):
        """The rendered loop when ready, else the still image, else no background."""
        if not capture.background or not Path(capture.background).is_file():
            return ''
        if capture.motion:
            width, height = map(int, capture.size.split('x'))
            try:
                mode = scene.resolve_fit(capture.background, capture.fit, width, height)
                loop = scene.cache_path(capture.background, width, height, capture.fps, mode)
            except OSError:
                return ''
            if loop.exists():
                return str(loop)
        return capture.background

    def start_preview(self):
        with socket.socket() as port_picker:
            port_picker.bind(('127.0.0.1', 0))
            port = port_picker.getsockname()[1]
        self.control_endpoint = f'tcp://127.0.0.1:{port}'
        self.controls = LiveControls(self.control_endpoint, self)
        self.controls.applied.connect(self.controls_applied)
        self.controls.failed.connect(self.controls_failed)
        self.controls.start()
        self.preview = QProcess(self)
        self.preview.readyReadStandardOutput.connect(self.read_frames)
        self.preview.readyReadStandardError.connect(self.read_preview_errors)
        self.preview.finished.connect(self.preview_finished)
        filters = self.image_filters(self.capture) + f",zmq=bind_address='tcp\\://127.0.0.1\\:{port}'"
        self.backdrop = self.backdrop_path(self.capture)
        self.fit = self.capture.fit
        if self.backdrop:
            # Person matting needs every frame in Python; same outputs as below.
            self.preview.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))
            self.preview.start(sys.executable, [
                '-m', 'phonecam.compositor', '--input', str(self.pipe), '--filters', filters,
                '--device', self.device, '--size', self.capture.size, '--fps', str(self.capture.fps),
                '--background', self.backdrop, '--preview', f'{FRAME_WIDTH}x{FRAME_HEIGHT}',
                '--mirror', str(int(self.capture.background_mirror)), '--fit', self.capture.fit,
            ])
            return
        graph = f"[0:v]{filters},split=2[webcam][ui];[ui]scale={FRAME_WIDTH}:{FRAME_HEIGHT}:force_original_aspect_ratio=decrease,pad={FRAME_WIDTH}:{FRAME_HEIGHT}:(ow-iw)/2:(oh-ih)/2[screen]"
        self.preview.start('ffmpeg', [
            '-y', '-hide_banner', '-loglevel', 'error', '-probesize', '32768', '-analyzeduration', '1',
            '-flags', 'low_delay', '-threads', '1', '-f', 'matroska', '-i', str(self.pipe),
            '-filter_complex_threads', '2', '-filter_complex', graph,
            '-map', '[webcam]', '-an', '-pix_fmt', 'yuv420p', '-c:v', 'rawvideo', '-threads', '1', '-fps_mode', 'passthrough', '-f', 'v4l2', self.device,
            '-map', '[screen]', '-an',
            '-fps_mode', 'passthrough', '-pix_fmt', 'rgb24', '-c:v', 'rawvideo', '-threads', '1',
            '-flush_packets', '1', '-f', 'rawvideo', 'pipe:1',
        ])

    def read_logs(self):
        if self.source:
            output = bytes(self.source.readAllStandardOutput()).decode(errors='replace')
            if self.capture and self.capture.url:
                output = output.replace(self.capture.url, '[stream]')
            self.logs = (self.logs + output)[-10000:]

    def read_preview_errors(self):
        if self.preview:
            output = bytes(self.preview.readAllStandardError()).decode(errors='replace')
            self.logs = (self.logs + output)[-10000:]

    def read_frames(self):
        if not self.preview or self.stopping or self.closing:
            return
        self.buffer.extend(bytes(self.preview.readAllStandardOutput()))
        count = len(self.buffer) // FRAME_BYTES
        if not count:
            return
        start = (count - 1) * FRAME_BYTES
        data = bytes(self.buffer[start:start + FRAME_BYTES])
        del self.buffer[:count * FRAME_BYTES]
        image = QImage(data, FRAME_WIDTH, FRAME_HEIGHT, FRAME_WIDTH * 3, QImage.Format.Format_RGB888).copy()
        self.frame.emit(image)
        if not self.received:
            self.received = True
            self.good = replace(self.capture)
            self.first_frame_timer.stop()
            self.state.emit('Σε σύνδεση')
            self.working.emit(replace(self.capture))
            self.count_since = time.monotonic()
            self.count = 0
        self.count += count
        elapsed = time.monotonic() - self.count_since
        if elapsed >= 3:
            self.measured_fps.emit(self.count / elapsed)
            self.count = 0
            self.count_since = time.monotonic()

    def stop_preview(self):
        controls, self.controls = self.controls, None
        if controls:
            controls.close()
            controls.deleteLater()
        process, self.preview = self.preview, None
        if process:
            process.blockSignals(True)
            process.kill()
            process.waitForFinished(700)
            process.deleteLater()
        self.buffer.clear()

    def controls_applied(self, generation, capture):
        if generation == self.generation and self.want_capture and not self.stopping:
            self.capture = replace(capture)
            self.good = replace(capture)
            self.working.emit(replace(capture))

    def controls_failed(self, generation, message):
        if generation in (-1, self.generation) and self.want_capture and not self.stopping:
            self.control_error.emit(message)

    def preview_finished(self, *_):
        process, self.preview = self.preview, None
        if process:
            process.deleteLater()
        if self.want_capture and not self.stopping and self.source and self.source.state() == QProcess.ProcessState.Running:
            self.source.kill()

    def retry_preview(self):
        if self.want_capture and not self.stopping and not self.preview and not self.closing:
            self.buffer.clear()
            self.start_preview()

    def source_error(self, kind):
        if kind == QProcess.ProcessError.FailedToStart:
            self.source_finished(1, None)

    def source_finished(self, code, _):
        self.read_logs()
        source, self.source = self.source, None
        if source:
            source.deleteLater()
        self.first_frame_timer.stop()
        self.stop_preview()
        if self.closing or not self.want_capture:
            self.state.emit('Αποσυνδεδεμένο')
            return
        if self.pending:
            pending = self.pending
            generation = self.generation
            QTimer.singleShot(500, lambda: self.begin(pending) if self.generation == generation else None)
        elif code and self.good and self.capture != self.good:
            good = replace(self.good)
            self.restored.emit(good)
            generation = self.generation
            QTimer.singleShot(500, lambda: self.begin(good) if self.generation == generation else None)
        else:
            self.want_capture = False
            self.state.emit('Αποσυνδεδεμένο')
            message = 'Η κάμερα σταμάτησε. Έλεγξε τη σύνδεση του κινητού και πάτησε Σύνδεση.'
            if self.capture and self.capture.source == 'network':
                message = ('Η ροή από το κινητό σταμάτησε. Η εφαρμογή κάμερας στο κινητό πρέπει να μένει ανοιχτή '
                           '(με κλειστή οθόνη κάποιες σταματούν — άφησέ τη σε πρώτο πλάνο) και στο ίδιο Wi-Fi. Πάτησε ξανά Σύνδεση.')
            if self.logs:
                useful = [line for line in self.logs.splitlines() if any(word in line.lower() for word in ('error', 'failed', 'disconnected', 'busy'))]
                if useful:
                    message += '\n\n' + '\n'.join(useful[-3:])[:600]
            self.failure.emit(message)

    def start_timeout(self):
        if not self.received and self.source:
            self.logs += '\nERROR: Δεν έφτασε εικόνα. Έλεγξε την εφαρμογή/διεύθυνση ροής ή την κάμερα του κινητού.'
            self.source.kill()

    def stop(self):
        self.generation += 1
        self.want_capture = False
        self.pending = None
        self.first_frame_timer.stop()
        self.stopping = True
        if self.source:
            self.source.terminate()
            process = self.source
            QTimer.singleShot(1500, lambda: process.kill() if self.source is process and process.state() != QProcess.ProcessState.NotRunning else None)
        self.stop_preview()
        self.state.emit('Αποσυνδεδεμένο')

    def close(self):
        self.closing = True
        self.stop()
        if self.source and not self.source.waitForFinished(2500):
            self.source.kill()
            self.source.waitForFinished(1000)
