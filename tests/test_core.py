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
