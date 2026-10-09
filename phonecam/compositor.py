"""Virtual background: matte the person out of each frame and place them in a scene.

Runs as its own process in place of the preview FFmpeg, with the same contract:
filtered frames go to the virtual camera, 960x540 RGB previews go to stdout.
"""
import argparse
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
import cv2
import numpy as np
import json
from pathlib import Path
from .scene import fit_graph, frame_still, guided, is_video, resolve_fit
from .scene import CACHE
from .weights import PERSON

MATTE_SIZE = (256, 144)
GUIDE_WIDTH = 480


def die_with_parent():
    # Children must not outlive the compositor when the app kills it.
    try:
        ctypes.CDLL('libc.so.6').prctl(1, signal.SIGKILL)
    except OSError:
        pass


def spawn(arguments, **options):
    return subprocess.Popen(arguments, preexec_fn=die_with_parent, **options)


class Matte:
    """Person probability per pixel, steadied over time and snapped to edges."""

    def __init__(self, width, height):
        self.net = cv2.dnn.readNetFromTFLite(str(PERSON))
        self.width, self.height = width, height
        self.guide_size = (GUIDE_WIDTH, max(2, round(GUIDE_WIDTH * height / width)))
        self.previous = None

    def __call__(self, frame):
        small = cv2.resize(frame, MATTE_SIZE, interpolation=cv2.INTER_AREA)
        blob = cv2.dnn.blobFromImage(small, 1 / 255, swapRB=True)
        self.net.setInput(blob)
        mask = np.ascontiguousarray(self.net.forward()[0, 0], dtype=np.float32)
        if self.previous is not None:
            # Follow movement quickly, but keep still edges from shimmering.
            change = np.abs(mask - self.previous)
            keep = np.clip(0.65 - change * 2.5, 0.1, 0.65)
            mask = self.previous * keep + mask * (1 - keep)
        self.previous = mask
        guide = cv2.cvtColor(cv2.resize(frame, self.guide_size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32) / 255
        refined = guided(guide, cv2.resize(mask, self.guide_size, interpolation=cv2.INTER_LINEAR), 7, 1e-3)
        # Firm up the matte: hair-soft edges, no transparent bodies.
        refined = np.clip((refined - 0.3) / 0.4, 0, 1)
        refined = refined * refined * (3 - 2 * refined)
        return cv2.resize(refined, (self.width, self.height), interpolation=cv2.INTER_LINEAR)


class Backdrop:
    """Animated loop through FFmpeg, or a still image."""

    def __init__(self, path, width, height, fit='cover'):
        self.width, self.height = width, height
        self.process = None
        self.still = None
        self.size = width * height * 3
        # Rendered loops are already framed; only original files need fitting here.
        mode = 'cover' if Path(path).parent == CACHE else resolve_fit(path, fit, width, height)
        if is_video(path):
            self.process = spawn([
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-stream_loop', '-1', '-i', path,
                '-filter_complex', fit_graph(mode, width, height, '[0:v]', '[out]'), '-map', '[out]',
                '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1',
            ], stdout=subprocess.PIPE, bufsize=0)
        else:
            image = cv2.imread(path, cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('Δεν διαβάστηκε η εικόνα φόντου.')
            self.still = frame_still(image, width, height, mode)
        self.last = self.still

    def next(self):
        if self.process:
            data = read_exact(self.process.stdout, self.size)
            if data is not None:
                self.last = np.frombuffer(data, np.uint8).reshape(self.height, self.width, 3)
        return self.last

    def close(self):
        if self.process:
            self.process.kill()


def read_exact(stream, size):
    buffer = bytearray(size)
    view = memoryview(buffer)
    done = 0
    while done < size:
        count = stream.readinto(view[done:])
        if not count:
            return None
        done += count
    return buffer


def preview_frame(frame, width, height):
    h, w = frame.shape[:2]
    scale = min(width / w, height / h)
    size = (max(2, round(w * scale)), max(2, round(h * scale)))
    small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    canvas = np.zeros((height, width, 3), np.uint8)
    y, x = (height - size[1]) // 2, (width - size[0]) // 2
    canvas[y:y + size[1], x:x + size[0]] = small
    return cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--filters', required=True)
    parser.add_argument('--device', required=True)
    parser.add_argument('--size', required=True)
    parser.add_argument('--fps', type=int, required=True)
    parser.add_argument('--background', required=True)
    parser.add_argument('--preview', default='960x540')
    parser.add_argument('--mirror', type=int, default=0)
    parser.add_argument('--fit', default='auto')
    parser.add_argument('--format', default='v4l2', help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    width, height = map(int, arguments.size.split('x'))
    preview_width, preview_height = map(int, arguments.preview.split('x'))
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    cv2.setNumThreads(4)
    decoder = spawn([
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-probesize', '32768', '-analyzeduration', '1',
        '-flags', 'low_delay', '-threads', '1', '-f', 'matroska', '-i', arguments.input,
        '-filter_complex_threads', '2', '-filter_complex', f'[0:v]{arguments.filters}[out]',
        '-map', '[out]', '-an', '-fps_mode', 'passthrough', '-pix_fmt', 'bgr24', '-f', 'rawvideo', 'pipe:1',
    ], stdout=subprocess.PIPE, bufsize=0)
    encoder = spawn([
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{width}x{height}', '-r', str(arguments.fps), '-i', 'pipe:0',
        '-an', '-pix_fmt', 'yuv420p', '-c:v', 'rawvideo', '-f', arguments.format, arguments.device,
    ], stdin=subprocess.PIPE, bufsize=0)
    backdrop = Backdrop(arguments.background, width, height, arguments.fit)
    current = (arguments.background, arguments.fit)
    matte = Matte(width, height)
    # The scene has its own Mirror, independent of the person's; it changes live over stdin.
    state = {'mirror': bool(arguments.mirror), 'background': current}

    def listen():
        for line in sys.stdin:
            name, _, value = line.rstrip('\n').partition(' ')
            if name == 'mirror':
                state['mirror'] = value == '1'
            elif name == 'background' and value:
                path, fit = json.loads(value)
                state['background'] = (path, fit)

    threading.Thread(target=listen, daemon=True).start()
    output = sys.stdout.buffer
    size = width * height * 3
    slow_since = None
    try:
        while True:
            data = read_exact(decoder.stdout, size)
            if data is None:
                break
            frame = np.frombuffer(data, np.uint8).reshape(height, width, 3)
            started = time.monotonic()
            alpha = matte(frame)
            if state['background'] != current:
                current = state['background']
                try:
                    replacement = Backdrop(current[0], width, height, current[1])
                    backdrop.close()
                    backdrop = replacement
                except (OSError, ValueError) as exc:
                    print(f'Δεν άλλαξε το φόντο: {exc}', file=sys.stderr, flush=True)
            scenery = backdrop.next()
            if state['mirror']:
                scenery = cv2.flip(scenery, 1)
            composed = cv2.blendLinear(frame, scenery, alpha, 1 - alpha)
            encoder.stdin.write(composed.data)
            output.write(preview_frame(composed, preview_width, preview_height).data)
            output.flush()
            spent = time.monotonic() - started
            if spent > 1.2 / arguments.fps:
                slow_since = slow_since or started
                if started - slow_since > 5:
                    print(f'Το φόντο καθυστερεί ({spent * 1000:.0f} ms/καρέ). Δοκίμασε μικρότερη ανάλυση.', file=sys.stderr, flush=True)
                    slow_since = started
            else:
                slow_since = None
    except BrokenPipeError:
        pass
    finally:
        backdrop.close()
        for process in (decoder, encoder):
            process.kill()
    return 0


if __name__ == '__main__':
    sys.exit(main())
