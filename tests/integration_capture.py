"""Opt-in end-to-end checks. Requires a free v4l2loopback device.

python tests/integration_capture.py --device /dev/video11 [--phone SERIAL]
With no phone, serve a local MJPEG stream to exercise Android 10's HTTP path.
"""
import argparse
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication, QTimer
from phonecam.config import Preferences
from phonecam.engine import CaptureEngine
from phonecam.models import Capture


class Stream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace;boundary=ffmpeg')
        self.end_headers()
        process = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-re', '-f', 'lavfi', '-i', 'testsrc2=size=640x480:rate=15', '-an', '-c:v', 'mjpeg', '-threads', '1', '-q:v', '4', '-f', 'mpjpeg', 'pipe:1'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while chunk := process.stdout.read(16384):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', required=True)
    parser.add_argument('--phone')
    args = parser.parse_args()
    app = QCoreApplication([])
    server = None
    if args.phone:
        baseline = replace(Preferences().capture, source='android', serial=args.phone)
    else:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Stream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        baseline = Capture(source='network', url=f'http://127.0.0.1:{server.server_port}/video', size='640x480', fps=15)
    engine = CaptureEngine()
    stage = [0]
    gray_seen = [False]
    gray_delta = [None]
    restored = [False]
    result = [1]
    source_pid = [None]
    last_frame = [None]
    max_gap = [0]

    def fail(message):
        print('FAIL:', message, flush=True)
        app.quit()

    def frame(image):
        if stage[0] < 4:
            now = time.monotonic()
            if last_frame[0]:
                max_gap[0] = max(max_gap[0], now - last_frame[0])
            last_frame[0] = now
        if stage[0] == 1:
            samples = [image.pixelColor(x, y) for x in range(240, 720, 70) for y in range(140, 400, 60)]
            gray_delta[0] = max(max(c.red(), c.green(), c.blue()) - min(c.red(), c.green(), c.blue()) for c in samples)
            # YUV/RGB conversions and MJPEG full-range rounding can shift a
            # neutral channel by a few levels; saturated fixture colors exceed 100.
            gray_seen[0] = gray_delta[0] < 10

    def advance():
        if stage[0] == 0:
            stage[0] = 1
            engine.start(replace(baseline, look='mono', exposure=10), args.device)
        elif stage[0] == 1:
            if not gray_seen[0]:
                fail(f'Monochrome filter did not reach the preview (channel delta {gray_delta[0]}). ' + engine.logs[-1000:])
                return
            stage[0] = 2
            engine.start(replace(baseline, rotation=90, mirror=True, look='warm'), args.device)
        elif stage[0] == 2:
            expected = baseline.size.replace('x', '/')
            info = subprocess.run(['v4l2-ctl', '-d', args.device, '--get-fmt-video'], capture_output=True, text=True, timeout=3)
            if expected not in info.stdout:
                fail('Rotation changed the virtual camera dimensions: ' + info.stdout)
                return
            stage[0] = 3
            engine.start(baseline, args.device)
        elif stage[0] == 3 and args.phone:
            stage[0] = 4
            engine.start(replace(baseline, camera='invalid-camera-id'), args.device)
        else:
            if args.phone and not restored[0]:
                fail('Failed camera selection was not rolled back.')
                return
            if max_gap[0] > 0.6:
                fail(f'Live changes interrupted frames for {max_gap[0]:.3f}s')
                return
            print(f'PASS: live image changes, unchanged source process, max frame gap {max_gap[0]:.3f}s' + (', failure rollback' if args.phone else ', HTTP/MJPEG'), flush=True)
            result[0] = 0
            app.quit()

    def working(capture):
        if stage[0] == 0:
            source_pid[0] = engine.source.processId()
        elif stage[0] < 4 and engine.source.processId() != source_pid[0]:
            fail('Image controls restarted the capture source.')
            return
        print(f'Working stage {stage[0]}: {capture.source} {capture.size} {capture.look} rotation={capture.rotation} mirror={capture.mirror}', flush=True)
        QTimer.singleShot(1400, advance)

    engine.frame.connect(frame)
    engine.working.connect(working)
    engine.restored.connect(lambda *_: restored.__setitem__(0, True))
    engine.failure.connect(fail)
    engine.state.connect(lambda message: print(message, flush=True))
    QTimer.singleShot(45000, lambda: fail('Integration timeout'))
    engine.start(baseline, args.device)
    app.exec()
    engine.close()
    if server:
        server.shutdown()
        server.server_close()
    return result[0]


if __name__ == '__main__':
    raise SystemExit(main())
