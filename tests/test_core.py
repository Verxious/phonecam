import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
import numpy as np
import cv2
os.environ.setdefault('XDG_CONFIG_HOME', tempfile.mkdtemp(prefix='phonecam-test-'))
from phonecam import scene
from phonecam.compositor import Matte, preview_frame
from phonecam.models import Capture, parse_devices, parse_cameras
from phonecam.network import forwarded_url
from phonecam.engine import CaptureEngine


class CoreTests(unittest.TestCase):
    def test_devices_keep_unauthorized_and_transport(self):
        devices = parse_devices('List of devices attached\nabc unauthorized usb:1-2\n192.168.1.24:5555 device product:phone model:POCO_F6_Pro\n')
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0].state, 'unauthorized')
        self.assertIn('Wi-Fi', devices[1].label)
        self.assertIn('POCO F6 Pro', devices[1].label)

    def test_camera_metadata_excludes_high_speed_modes(self):
        cameras = parse_cameras('''--camera-id=0 (back, 4096x3072, fps={15, 24, 30}, zoom-range=[1,10])
        - 1920x1080
      High speed capture (--camera-high-speed):
        - 1280x720 (fps={120,240})
    --camera-id=wide-camera (back, 3280x2464, fps={15,30})
        - 1280x720
''')
        self.assertEqual(cameras[0].sizes, ['1920x1080'])
        self.assertEqual(cameras[1].identifier, 'wide-camera')
        self.assertEqual(cameras[1].sizes, ['1280x720'])

    def test_mirror_is_horizontal_after_rotation(self):
        self.assertEqual(Capture(rotation=90, mirror=True).orientation, 'flip270')
        self.assertEqual(Capture(rotation=270, mirror=True).orientation, 'flip90')

    def test_reject_invalid_capture(self):
        for values in ({'size':'1921x1080'}, {'fps':120}, {'source':'network','url':'file:///etc/passwd'}, {'look':'bad'}, {'exposure':30}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Capture(**values).validated()

    def test_usb_forward_preserves_stream_path_and_credentials(self):
        self.assertEqual(forwarded_url('http://user:pass@phone:8080/video?x=1', 42001), 'http://user:pass@127.0.0.1:42001/video?x=1')
        self.assertEqual(forwarded_url('rtsp://[::1]:554/live', 42002), 'rtsp://127.0.0.1:42002/live')

    def test_filters_apply_to_network_camera_geometry(self):
        graph = CaptureEngine.image_filters(None, Capture(source='network', rotation=90, mirror=True, look='mono', exposure=10))
        self.assertIn('rotate@turn=angle=90*PI/180', graph)
        self.assertIn('hflip@mirror=enable=1', graph)
        self.assertIn('brightness=0.1', graph)
        self.assertIn('saturation=0', graph)


def synthetic_scene():
    """Blue sky over a textured blue sea with a brown deck and a lamp."""
    rng = np.random.default_rng(1)
    image = np.zeros((180, 320, 3), np.uint8)
    image[:90] = (200, 150, 110)
    image[90:] = (150, 90, 40)
    image[90:] = np.clip(image[90:] + rng.integers(-30, 30, image[90:].shape), 0, 255).astype(np.uint8)
    image[150:] = (40, 70, 110)
    cv2.circle(image, (60, 140), 4, (120, 200, 255), -1)
    return image


class BackgroundTests(unittest.TestCase):
    def test_loop_is_seamless_and_deck_stays_still(self):
        info = scene.analyse(synthetic_scene())
        clip = scene.Scene(synthetic_scene(), 320, 180, 10, info, seconds=2)
        first, second, last = (clip.frame(index).astype(int) for index in (0, 1, clip.frames - 1))
        normal = np.abs(second - first).mean()
        seam = np.abs(first - last).mean()
        self.assertGreater(normal, 0)
        self.assertLess(seam, normal * 2 + 0.5)
        self.assertTrue(np.array_equal(clip.frame(0), clip.frame(clip.frames)))
        deck = info['foreground'] > 0.99
        self.assertTrue(deck[170].all())
        moving = np.abs(clip.frame(5).astype(int) - first).sum(2)
        self.assertLess(moving[deck & (np.arange(180)[:, None] > 165)].max(), 40)

    def test_backdrop_falls_back_to_still_then_uses_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'sea.png'
            cv2.imwrite(str(image), synthetic_scene())
            capture = Capture(size='320x180', fps=15, background=str(image))
            self.assertEqual(CaptureEngine.backdrop_path(capture), str(image))
            loop = scene.cache_path(image, 320, 180, 15)
            loop.parent.mkdir(parents=True, exist_ok=True)
            loop.write_bytes(b'x')
            try:
                self.assertEqual(CaptureEngine.backdrop_path(capture), str(loop))
                self.assertEqual(CaptureEngine.backdrop_path(Capture(size='320x180', fps=15, background=str(image), motion=False)), str(image))
                self.assertNotEqual(scene.cache_path(image, 640, 360, 15), loop)
            finally:
                loop.unlink()
        self.assertEqual(CaptureEngine.backdrop_path(Capture(background='/missing/background.png')), '')
        self.assertEqual(CaptureEngine.backdrop_path(Capture()), '')

    def test_video_loop_has_no_jump(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'walk.mp4'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=15,hue=h=t*60',
                '-t', '4', '-pix_fmt', 'yuv420p', str(source)], check=True)
            loop = scene.seamless(source, 160, 90, 15)
            try:
                capture = cv2.VideoCapture(str(loop))
                frames = []
                while (frame := capture.read())[0]:
                    frames.append(frame[1].astype(int))
                steps = [np.abs(frames[i] - frames[(i + 1) % len(frames)]).mean() for i in range(len(frames))]
                self.assertEqual(len(frames), 45)  # 4 s minus the 1 s dissolve
                self.assertLessEqual(steps[-1], np.median(steps) * 1.5)
            finally:
                loop.unlink()

    def test_library_removal_deletes_only_what_phonecam_made(self):
        from PySide6.QtWidgets import QApplication, QMessageBox
        from phonecam import library, youtube
        from phonecam.app import Window
        from phonecam.android import Android
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        app = QApplication.instance() or QApplication([])
        Android.refresh = lambda *_: None
        with tempfile.TemporaryDirectory() as folder:
            own = Path(folder) / 'mine.png'
            cv2.imwrite(str(own), synthetic_scene())
            youtube.FOLDER.mkdir(parents=True, exist_ok=True)
            fetched = youtube.FOLDER / 'clip [abcdefghijk].mp4'
            fetched.write_bytes(b'video')
            loops = [scene.cache_path(path, 320, 180, 15) for path in (own, fetched)]
            for loop in loops:
                loop.parent.mkdir(parents=True, exist_ok=True)
                loop.write_bytes(b'loop')
            window = Window()
            for path in (own, fetched):
                window.remember_background(str(path))
            window.capture = replace(window.capture, background=str(fetched))
            dialog = library.LibraryDialog(window)
            original = QMessageBox.question
            QMessageBox.question = staticmethod(lambda *_: QMessageBox.StandardButton.Yes)
            try:
                for path in (own, fetched):
                    dialog.refresh(select=str(path))
                    dialog.remove_selected()
            finally:
                QMessageBox.question = original
                dialog.done(0)
                window.engine.close()
            self.assertTrue(own.exists())            # the user's own file stays
            self.assertFalse(fetched.exists())       # downloaded video is deleted
            self.assertFalse(any(loop.exists() for loop in loops))
            self.assertEqual(window.preferences.data['backgrounds'], [])
            self.assertEqual(window.capture.background, '')

    def test_portrait_backgrounds_are_shown_whole(self):
        with tempfile.TemporaryDirectory() as folder:
            portrait = Path(folder) / 'tall.png'
            image = np.full((800, 400, 3), 200, np.uint8)
            cv2.imwrite(str(portrait), image)
            self.assertEqual(scene.resolve_fit(portrait, 'auto', 1920, 1080), 'bars')
            wide = Path(folder) / 'wide.png'
            cv2.imwrite(str(wide), np.full((540, 1000, 3), 200, np.uint8))
            self.assertEqual(scene.resolve_fit(wide, 'auto', 1920, 1080), 'cover')
            framed = scene.frame_still(image, 320, 180, 'bars')
            self.assertEqual(framed.shape, (180, 320, 3))
            self.assertEqual(int(framed[:, :100].max()), 0)        # black side bars
            self.assertGreater(int(framed[:, 150:170].min()), 150)  # whole picture in the middle
            blurred = scene.frame_still(image, 320, 180, 'blur')
            self.assertGreater(int(blurred[:, :40].mean()), 50)     # bars filled, not black
            video = Path(folder) / 'tall.mp4'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=white:size=180x320:rate=10',
                '-t', '1', '-pix_fmt', 'yuv420p', str(video)], check=True)
            self.assertEqual(scene.source_size(video), (180, 320))
            loop = scene.seamless(video, 320, 180, 10, fit='bars')
            try:
                ok, frame = cv2.VideoCapture(str(loop)).read()
                self.assertTrue(ok)
                self.assertLess(int(frame[:, :60].mean()), 20)
                self.assertGreater(int(frame[:, 140:180].mean()), 200)
            finally:
                loop.unlink()

    def test_missing_driver_is_offered_not_reported(self):
        from unittest import mock
        from PySide6.QtCore import QCoreApplication
        from phonecam import virtual
        app = QCoreApplication.instance() or QCoreApplication([])
        offered, errors = [], []
        camera = virtual.VirtualCamera()
        camera.driver_missing.connect(lambda: offered.append(True))
        camera.error.connect(errors.append)
        with mock.patch.object(virtual.Path, 'glob', return_value=[]):
            with mock.patch.object(virtual, 'available', return_value=False):
                camera.prepare()
            # Driver loaded by someone else, but v4l2loopback-ctl is not installed.
            real_exists = Path.exists
            with mock.patch.object(virtual, 'available', return_value=True), \
                 mock.patch.object(virtual, 'tool', return_value='v4l2loopback-ctl'), \
                 mock.patch.object(virtual.Path, 'exists', lambda self: True if str(self) == '/sys/module/v4l2loopback' else real_exists(self)):
                camera.prepare()
        self.assertEqual(offered, [True, True])
        self.assertEqual(errors, [])

    def test_stream_problems_are_explained(self):
        import socket
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from phonecam.network import diagnose

        class Phone(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == '/video':
                    self.send_response(200)
                    self.send_header('Content-Type', 'multipart/x-mixed-replace;boundary=frame')
                    self.end_headers()
                    self.wfile.write(b'--frame\r\n')
                elif self.path == '/locked':
                    self.send_response(401)
                    self.end_headers()
                elif self.path == '/':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.end_headers()
                    self.wfile.write(b'<html>IP Webcam</html>')
                else:
                    self.send_response(404)
                    self.end_headers()

        server = ThreadingHTTPServer(('127.0.0.1', 0), Phone)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            self.assertEqual(diagnose(base + '/video'), '')
            self.assertIn('/video', diagnose(base + '/'))           # the app's page, not the stream
            self.assertIn('δεν υπάρχει', diagnose(base + '/wrong'))
            self.assertIn('κωδικό', diagnose(base + '/locked'))
        finally:
            server.shutdown()
        with socket.socket() as free:
            free.bind(('127.0.0.1', 0))
            closed = free.getsockname()[1]
        self.assertIn('Start server', diagnose(f'http://127.0.0.1:{closed}/video'))
        self.assertIn('ίδιο Wi-Fi', diagnose('http://192.0.2.1:8080/video', timeout=1))  # TEST-NET: never answers

    def test_loop_quality_never_forces_a_rerender(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'sea.png'
            cv2.imwrite(str(image), synthetic_scene())
            high = scene.cache_path(image, 320, 180, 15)
            small = scene.cache_path(image, 320, 180, 15, quality='small')
            self.assertNotEqual(high, small)
            self.assertIsNone(scene.ready_loop(image, 320, 180, 15, quality='small'))
            high.parent.mkdir(parents=True, exist_ok=True)
            high.write_bytes(b'loop')
            try:
                # Asking for another quality reuses the loop that exists.
                self.assertEqual(scene.ready_loop(image, 320, 180, 15, quality='small'), high)
                small.write_bytes(b'loop')
                self.assertEqual(scene.ready_loop(image, 320, 180, 15, quality='small'), small)
                self.assertIn(small, scene.derived_files(image))
            finally:
                high.unlink(missing_ok=True)
                small.unlink(missing_ok=True)

    def test_sound_plays_only_the_chosen_seconds(self):
        import time
        from PySide6.QtCore import QCoreApplication, QTimer
        from phonecam import sound
        if not sound.available():
            self.skipTest('no PulseAudio/PipeWire here')
        app = QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as folder:
            tone = Path(folder) / 'tone.ogg'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=10',
                str(tone)], check=True)
            item = sound.Sound('τόνος', str(tone), start=4, length=0.6, volume=1)
            self.assertEqual(sound.Sound.from_dict({**item.to_dict(), 'unknown': 1}), item)
            player = sound.Player()
            ended = []
            player.stopped.connect(lambda _: (ended.append(time.monotonic()), app.quit()))
            started = time.monotonic()
            player.play(item, to_mic=False)
            QTimer.singleShot(5000, app.quit)
            app.exec()
            self.assertTrue(ended)
            self.assertLess(ended[0] - started, 3)  # 0.6 s of a 10 s file, not the whole file

    def test_soundboard_waveform_drag_and_pad_keys(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication
        from phonecam import sound, soundboard
        from phonecam.android import Android
        from phonecam.app import Window
        app = QApplication.instance() or QApplication([])
        wave = soundboard.Waveform(10.0, 0.0, 5.0, '#ff4d6d')
        wave.resize(1000, 100)
        spans = []
        wave.changed.connect(lambda start, length: spans.append((round(start, 1), round(length, 1))))
        QTest.mousePress(wave, Qt.MouseButton.LeftButton, pos=QPoint(200, 50))   # 2.0 s
        QTest.mouseMove(wave, QPoint(450, 50))                                   # 4.5 s
        QTest.mouseRelease(wave, Qt.MouseButton.LeftButton, pos=QPoint(450, 50))
        self.assertEqual(spans[-1], (2.0, 2.5))
        QTest.mousePress(wave, Qt.MouseButton.LeftButton, pos=QPoint(451, 50))   # grab the right edge
        QTest.mouseMove(wave, QPoint(700, 50))
        QTest.mouseRelease(wave, Qt.MouseButton.LeftButton, pos=QPoint(700, 50))
        self.assertEqual(spans[-1], (2.0, 5.0))
        # Zoomed in on 2-4 s, the same pixels mean finer seconds.
        wave.show_span(2.5, 1.0)
        self.assertEqual(wave.view, (2.0, 4.0))
        QTest.mousePress(wave, Qt.MouseButton.LeftButton, pos=QPoint(250, 50))   # 2.5 s
        QTest.mouseMove(wave, QPoint(750, 50))                                   # 3.5 s
        QTest.mouseRelease(wave, Qt.MouseButton.LeftButton, pos=QPoint(750, 50))
        self.assertEqual(spans[-1], (2.5, 1.0))
        jumps = []
        wave.seek.connect(jumps.append)
        QTest.mouseClick(wave, Qt.MouseButton.LeftButton, pos=QPoint(500, 50))   # a click jumps, no selection
        self.assertEqual(round(jumps[-1], 1), 3.0)
        self.assertEqual(spans[-1], (2.5, 1.0))
        Android.refresh = lambda *_: None
        window = Window()
        played = []
        window.player.play = lambda item, to_mic=True: played.append(item.name)
        window.sounds = [sound.Sound('πρώτος', '/x.ogg'), sound.Sound('δεύτερος', '/y.ogg')]
        window.soundboard.rebuild()
        window.show_tab(1)
        QTest.keyClick(window.soundboard, Qt.Key.Key_2)
        QTest.keyClick(window.soundboard, Qt.Key.Key_1)
        self.assertEqual(played, ['δεύτερος', 'πρώτος'])
        # Greek layout: physical key W (scan code 25) types «ς»; it is still pad 12's key.
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        window.sounds = [sound.Sound(f'pad {number}', '/x.ogg') for number in range(1, 13)]
        window.soundboard.rebuild()
        played.clear()
        event = QKeyEvent(QEvent.Type.KeyPress, 0, Qt.KeyboardModifier.NoModifier, 25, 0, 0, 'ς')
        app.sendEvent(window.soundboard, event)
        self.assertEqual(played, ['pad 12'])
        window.engine.close()

    def test_matte_and_preview_shapes(self):
        frame = np.full((360, 640, 3), 90, np.uint8)
        alpha = Matte(640, 360)(frame)
        self.assertEqual(alpha.shape, (360, 640))
        self.assertGreaterEqual(float(alpha.min()), 0)
        self.assertLessEqual(float(alpha.max()), 1)
        preview = preview_frame(np.zeros((1080, 1440, 3), np.uint8), 960, 540)
        self.assertEqual(preview.shape, (540, 960, 3))

    def test_background_fields_are_validated(self):
        with self.assertRaises(ValueError):
            Capture(background=None).validated()
        self.assertTrue(Capture(background='/x.png', motion=False).validated().background)


if __name__ == '__main__':
    unittest.main()
