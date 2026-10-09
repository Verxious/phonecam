"""Seamless animated loops rendered once from a still background image.

The image is split automatically into sky, sea and static foreground. Sky and sea
are advected with two-phase flow maps (so the loop has no visible jump), drifting
mist sits over the horizon and warm light sources flicker. Everything is periodic
in the loop length, and the rendered loop is cached as a video next to the settings.
"""
import functools
import hashlib
from multiprocessing import get_context
import os
from pathlib import Path
import re
import subprocess
import cv2
import numpy as np
from .config import STATE

VERSION = 2
VIDEO_VERSION = 1
VIDEOS = ('.mp4', '.mkv', '.webm', '.mov', '.m4v', '.avi', '.gif')
LOOP_SECONDS = 12
CACHE = STATE / 'backgrounds'
TAU = 2 * np.pi


def cover(image, width, height):
    """Scale and centre-crop to fill the output exactly."""
    h, w = image.shape[:2]
    scale = max(width / w, height / h)
    resized = cv2.resize(image, (max(width, round(w * scale)), max(height, round(h * scale))), interpolation=cv2.INTER_LANCZOS4 if scale > 1 else cv2.INTER_AREA)
    y = (resized.shape[0] - height) // 2
    x = (resized.shape[1] - width) // 2
    return np.ascontiguousarray(resized[y:y + height, x:x + width])


FITS = ('auto', 'cover', 'bars', 'blur')


def source_size(path):
    """Displayed (width, height) of an image or video, honouring phone rotation tags."""
    stat = Path(path).stat()
    return _source_size(str(path), stat.st_mtime_ns, stat.st_size)


@functools.lru_cache(maxsize=64)
def _source_size(path, *_):
    # Cached: the engine asks on every filter change, and probing runs FFmpeg.
    if not is_video(path):
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError('Δεν διαβάστηκε η εικόνα φόντου.')
        return image.shape[1], image.shape[0]
    result = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(path)], capture_output=True, text=True, timeout=20)
    match = re.search(r'Video:.*?(\d{2,5})x(\d{2,5})', result.stderr)
    if not match:
        raise ValueError('Δεν διαβάστηκε το βίντεο φόντου.')
    width, height = int(match[1]), int(match[2])
    rotation = re.search(r'rotation of (-?[\d.]+)', result.stderr)
    if rotation and round(abs(float(rotation[1]))) % 180 == 90:
        width, height = height, width
    return width, height


def resolve_fit(path, fit, width, height):
    """'auto' fills the screen when shapes are close and shows everything otherwise."""
    if fit in ('cover', 'bars', 'blur'):
        return fit
    try:
        source_width, source_height = source_size(path)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 'cover'
    ratio = (source_width / source_height) / (width / height)
    return 'cover' if 0.8 <= ratio <= 1.25 else 'bars'


def inner_size(path, width, height):
    """Size of the whole picture when it is fitted inside the frame (even numbers)."""
    source_width, source_height = source_size(path)
    scale = min(width / source_width, height / source_height)
    return max(2, int(source_width * scale) // 2 * 2), max(2, int(source_height * scale) // 2 * 2)


def fit_graph(mode, width, height, source, output):
    """FFmpeg filter graph that frames ``source`` into width x height."""
    fill = f'scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}'
    whole = f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2'
    if mode == 'bars':
        return f'{source}{whole},pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1{output}'
    if mode == 'blur':
        return (f'{source}split[fitback][fitfront];[fitback]scale={width // 4}:{height // 4}:force_original_aspect_ratio=increase,'
                f'crop={width // 4}:{height // 4},boxblur=12:3,eq=brightness=-0.12:saturation=0.85,scale={width}:{height}[fitblur];'
                f'[fitfront]{whole}[fitsharp];[fitblur][fitsharp]overlay=(W-w)/2:(H-h)/2,setsar=1{output}')
    return f'{source}{fill},setsar=1{output}'


def frame_still(image, width, height, mode):
    """Python version of fit_graph for still images."""
    if mode == 'cover':
        return cover(image, width, height)
    h, w = image.shape[:2]
    scale = min(width / w, height / h)
    size = (max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2))
    front = cv2.resize(image, size, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    if mode == 'blur':
        canvas = cv2.GaussianBlur(cover(image, width // 4, height // 4), (0, 0), 6)
        canvas = cv2.resize(cv2.convertScaleAbs(canvas, alpha=0.85), (width, height), interpolation=cv2.INTER_LINEAR)
    else:
        canvas = np.zeros((height, width, 3), np.uint8)
    y, x = (height - size[1]) // 2, (width - size[0]) // 2
    canvas[y:y + size[1], x:x + size[0]] = front
    return canvas


def tile_noise(height, width, scale, seed):
    """Smooth noise that wraps in both axes, normalised to 0..1."""
    rng = np.random.default_rng(seed)
    fy = np.fft.fftfreq(height)[:, None] * height
    fx = np.fft.fftfreq(width)[None, :] * width
    # ``scale`` is roughly how many blobs fit across the width.
    radius = np.sqrt((fx / scale) ** 2 + (fy / (scale * height / width)) ** 2)
    spectrum = np.exp(-(radius ** 2)) * (radius > 0)
    phases = rng.uniform(0, TAU, (height, width))
    field = np.real(np.fft.ifft2(spectrum * np.exp(1j * phases))).astype(np.float32)
    field -= field.min()
    return field / max(float(field.max()), 1e-6)


def guided(guide, source, radius, eps):
    """Edge-aware refinement (He et al.): coarse masks snap to image edges."""
    box = lambda value: cv2.boxFilter(value, -1, (radius, radius))
    mean_i, mean_p = box(guide), box(source)
    a = (box(guide * source) - mean_i * mean_p) / (box(guide * guide) - mean_i * mean_i + eps)
    b = mean_p - a * mean_i
    return np.clip(box(a) * guide + box(b), 0, 1)


def fill(image, mask, radius):
    """Spread region colours into the rest so warps never pull foreground in."""
    weight = cv2.GaussianBlur(mask, (0, 0), radius) + 1e-4
    spread = cv2.GaussianBlur(image * mask[..., None], (0, 0), radius) / weight[..., None]
    return image * mask[..., None] + spread * (1 - mask[..., None])


SKY = (2,)
WATER = (21, 26, 60, 128)  # water, sea, river, lake


def semantic(image, model):
    """ADE20K classes as soft sky / water probabilities at image size."""
    height, width = image.shape[:2]
    scale = 768 / max(width, height)
    size = (max(32, round(width * scale / 32) * 32), max(32, round(height * scale / 32) * 32))
    rgb = cv2.cvtColor(cv2.resize(image, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB).astype(np.float32) / 255
    blob = ((rgb - (0.485, 0.456, 0.406)) / (0.229, 0.224, 0.225)).transpose(2, 0, 1)[None].astype(np.float32)
    net = cv2.dnn.readNetFromONNX(str(model))
    net.setInput(blob)
    logits = net.forward()[0].astype(np.float32)
    logits -= logits.max(0, keepdims=True)
    probabilities = np.exp(logits)
    probabilities /= probabilities.sum(0, keepdims=True)
    grow = lambda plane: cv2.resize(plane, (width, height), interpolation=cv2.INTER_CUBIC)
    return np.clip(grow(probabilities[list(SKY)].sum(0)), 0, 1), np.clip(grow(probabilities[list(WATER)].sum(0)), 0, 1)


def horizon_row(sky, water, height):
    """First row where water outweighs sky across the frame."""
    rows_sky, rows_water = sky.mean(1), water.mean(1)
    for y in range(int(height * 0.2), int(height * 0.95)):
        if rows_water[y] > rows_sky[y] and rows_water[y] > 0.15:
            return y
    return int(height * 0.55)


def analyse(image, model=None):
    height, width = image.shape[:2]
    pixels = image.astype(np.float32) / 255
    blue, green, red = cv2.split(pixels)
    # Pixel-accurate "open air": sky and sea read cool, wood/cloth/skin/rope read warm.
    open_air = ((blue - red) > 0.035).astype(np.uint8)
    open_air = cv2.morphologyEx(open_air, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    if model:
        sky_class, water_class = semantic(image, model)
    else:
        rows = np.arange(height, dtype=np.float32)[:, None] / height * np.ones((1, width), np.float32)
        sky_class, water_class = (rows < 0.55).astype(np.float32), (rows >= 0.55).astype(np.float32)
    horizon = horizon_row(sky_class, water_class, height)
    # Foreground is the solid shape that reaches the bottom edge (deck, rails, people).
    solid = 1 - open_air
    count, labels = cv2.connectedComponents(solid, connectivity=8)
    touching = list(set(np.unique(labels[-2:, :])) - {0})
    foreground = np.isin(labels, touching).astype(np.float32)
    # With a scene model its classes are trusted (dark storm clouds are not "blue");
    # the colour test alone would freeze them in place.
    free = (1 - foreground) if model else open_air * (1 - foreground)
    gray = cv2.cvtColor(pixels, cv2.COLOR_BGR2GRAY)
    radius = max(5, height // 90)
    # The scene model is coarse; pull moving regions slightly back from solid land so
    # peaks and cliffs never ride along with clouds or water.
    # Next to the deck the pixel-accurate foreground already separates them, so keep
    # the water between balusters moving.
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    near_deck = cv2.dilate(foreground, kernel, iterations=3) > 0
    shrink = lambda mask: np.where(near_deck, mask, cv2.erode(mask, kernel))
    sky = free * guided(gray, shrink(np.clip((sky_class - 0.5) * 4, 0, 1)), radius, 2e-3)
    sea = free * guided(gray, shrink(np.clip((water_class - 0.5) * 4, 0, 1)), radius, 2e-3)
    # Small warm, bright blobs: lanterns, fires, windows.
    warm = (red - blue) * np.maximum(red, green)
    hot = ((warm > 0.18) & (red > 0.62)).astype(np.uint8)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(hot)
    lights = []
    for index in range(1, count):
        area = stats[index, cv2.CC_STAT_AREA]
        if 25 <= area <= width * height * 0.004:
            lights.append((float(centroids[index][0]), float(centroids[index][1]), float(np.sqrt(area))))
    lights = merge_lights(lights, height / 30)[:8]
    soften = lambda mask: np.clip(cv2.GaussianBlur(mask.astype(np.float32), (0, 0), 1.2), 0, 1)
    return {
        'horizon': horizon,
        'sky': soften(sky),
        'sea': soften(sea),
        'foreground': soften(foreground),
        'lights': lights,
    }


def merge_lights(lights, distance):
    """One flicker per lamp, not one per bright pixel cluster."""
    merged = []
    for x, y, size in sorted(lights, key=lambda item: -item[2]):
        for index, (mx, my, msize) in enumerate(merged):
            if (x - mx) ** 2 + (y - my) ** 2 < distance ** 2:
                total = msize ** 2 + size ** 2
                merged[index] = ((mx * msize ** 2 + x * size ** 2) / total, (my * msize ** 2 + y * size ** 2) / total, np.sqrt(total))
                break
        else:
            merged.append((x, y, size))
    return merged


class Scene:
    def __init__(self, image, width, height, fps, info=None, seconds=LOOP_SECONDS):
        self.width, self.height = width, height
        self.frames = max(1, round(fps * seconds))
        base = cover(image, width, height)
        self.base = base.astype(np.float32) / 255
        info = info or analyse(base)
        self.info = info
        self.sky, self.sea, self.foreground = info['sky'], info['sea'], info['foreground']
        self.background = 1 - self.foreground
        horizon = info['horizon']
        unit = height / 1080
        # Plates only take colour from confidently open pixels, so edges do not smear.
        self.sky_plate = fill(self.base, (self.sky > 0.9).astype(np.float32), 14 * unit)
        self.sea_plate = fill(self.base, (self.sea > 0.9).astype(np.float32), 14 * unit)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        self.xs, self.ys = xs, ys
        small = lambda scale, seed: cv2.resize(tile_noise(270, 480, scale, seed), (width, height), interpolation=cv2.INTER_CUBIC)
        # Clouds: high clouds move faster than clouds near the horizon (parallax).
        height_factor = np.clip(1 - ys / max(horizon, 1), 0, 1)
        variation = small(5, 11) - 0.5
        self.sky_flow_x = (24 + 40 * height_factor + 14 * variation) * unit
        self.sky_flow_y = (3 * variation) * unit
        self.sky_offset = small(2, 12)
        self.billow = small(3, 18) * TAU
        # Sea: travelling waves on a perspective ground plane, rolling toward the viewer.
        # Pure displacement (no cross-fade), so foam stays crisp.
        depth = np.clip((ys - horizon) / max(height - horizon, 1), 0, 1)
        self.depth = depth
        distance = 1 / (depth + 0.04)
        ground_x = (xs - width / 2) / height * distance
        jitter = small(3, 13) * TAU
        # (cycles across ground x, cycles per unit distance, cycles per loop, screen amplitude px)
        self.waves = []
        for kx, kz, cycles, amplitude, seed in ((0.6, 0.55, 9, 5.5, 0), (-1.4, 1.1, 14, 3.0, 1), (2.3, 1.7, 19, 1.8, 2)):
            phase = TAU * (kx * ground_x + kz * distance) + jitter * (0.6 + 0.3 * seed)
            self.waves.append((phase.astype(np.float32), cycles, amplitude * unit))
        self.wave_scale = (0.08 + depth ** 1.15).astype(np.float32)
        self.wave_phase = small(6, 15) * TAU
        # Mist band over the horizon and island base, thinning over the water.
        band = np.exp(-((ys - horizon * 0.96) / (height * 0.075)) ** 2) * np.where(ys > horizon, np.exp(-(ys - horizon) / (height * 0.05)), 1)
        self.mist_alpha = (band * 0.4 + np.exp(-((ys - horizon * 0.78) / (height * 0.07)) ** 2) * 0.14) * self.background
        self.mist_noise = tile_noise(270, 960, 9, 16)
        self.mist_noise2 = tile_noise(270, 960, 4, 17)
        sample = self.base[max(0, horizon - int(40 * unit)):horizon + int(10 * unit)]
        self.mist_colour = np.clip(np.percentile(sample.reshape(-1, 3), 80, axis=0) * 1.05, 0, 1).astype(np.float32)
        # Light flicker masks.
        self.lights = []
        for x, y, size in info['lights']:
            radius = max(size * 2.6, 9 * unit)
            glow = np.exp(-(((xs - x) ** 2 + (ys - y) ** 2) / (2 * radius ** 2))).astype(np.float32)
            core = np.exp(-(((xs - x) ** 2 + (ys - y) ** 2) / (2 * (radius * 0.45) ** 2))).astype(np.float32)
            self.lights.append((glow, core, len(self.lights)))
        self.light_colour = np.array([0.32, 0.62, 1.0], np.float32)

    def advect(self, plate, flow_x, flow_y, offset, cycles, t):
        """Two-phase flow map: continuous drift that loops without a jump."""
        result = np.zeros_like(plate)
        for shift in (0.0, 0.5):
            phase = np.mod(t * cycles + offset + shift, 1.0)
            weight = 1 - np.abs(2 * phase - 1)
            displacement = phase - 0.5
            warped = cv2.remap(plate, self.xs - flow_x * displacement, self.ys - flow_y * displacement, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            result += warped * weight[..., None]
        return result

    def frame(self, index):
        t = (index % self.frames) / self.frames
        unit = self.height / 1080
        frame = self.base.copy()
        # Clouds billow slowly while the flow map drifts them across the sky.
        billow_x = (4 * unit) * np.sin(self.billow + TAU * t)
        billow_y = (2.5 * unit) * np.cos(self.billow * 1.3 + TAU * t)
        sky_plate = cv2.remap(self.sky_plate, self.xs + billow_x, self.ys + billow_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        sky = self.advect(sky_plate, self.sky_flow_x, self.sky_flow_y, self.sky_offset, 1, t)
        dx = np.zeros_like(self.xs)
        dy = np.zeros_like(self.ys)
        shade = np.zeros_like(self.xs)
        for phase, cycles, amplitude in self.waves:
            angle = phase + TAU * cycles * t
            dy += amplitude * np.sin(angle)
            dx += 0.45 * amplitude * np.cos(angle)
            shade += np.cos(angle) * amplitude
        dx *= self.wave_scale
        dy *= self.wave_scale
        sea = cv2.remap(self.sea_plate, self.xs + dx, self.ys + dy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        # Wave faces catch the light as they roll in; brighter foam glints more.
        light = cv2.cvtColor(sea, cv2.COLOR_BGR2GRAY)
        sea *= (1 + 0.012 * shade / unit * self.wave_scale)[..., None]
        glint = np.clip((light - 0.55) * 3, 0, 1) * (0.5 + 0.5 * np.sin(self.wave_phase * 3 + TAU * 8 * t + self.xs / (23 * unit)))
        sea += (glint * 0.06)[..., None]
        frame = frame * (1 - self.sky[..., None]) + sky * self.sky[..., None]
        frame = frame * (1 - self.sea[..., None]) + sea * self.sea[..., None]
        # Mist: two layers drifting at different speeds, looping horizontally.
        h, w = self.mist_noise.shape
        shift_a = int(round(t * w)) % w
        shift_b = int(round(-t * w)) % w
        layer = np.roll(self.mist_noise, -shift_a, axis=1) * 0.65 + np.roll(self.mist_noise2, -shift_b, axis=1) * 0.35
        layer = cv2.resize(layer[:, :w // 2], (self.width, self.height), interpolation=cv2.INTER_CUBIC)
        alpha = self.mist_alpha * np.clip((layer - 0.25) * 1.6, 0, 1)
        frame = frame * (1 - alpha[..., None]) + self.mist_colour * alpha[..., None]
        # Flicker: several incommensurate-looking but loop-periodic sines.
        for glow, core, number in self.lights:
            a, b, c = 11 + number, 17 + 2 * number, 29 + 3 * number
            flicker = 0.5 * np.sin(TAU * a * t + number) + 0.3 * np.sin(TAU * b * t + 2 * number) + 0.2 * np.sin(TAU * c * t)
            gain = 0.10 + 0.07 * flicker
            frame += (glow * gain)[..., None] * self.light_colour * 0.6
            frame *= 1 + (core * 0.12 * flicker)[..., None]
        return (np.clip(frame, 0, 1) * 255 + 0.5).astype(np.uint8)


def is_video(path):
    return Path(path).suffix.lower() in VIDEOS


def source_tag(image_path):
    """Every file derived from one background starts with this, so it can be cleaned up."""
    return hashlib.sha256(str(Path(image_path).resolve()).encode()).hexdigest()[:10]


# x264 CRF per quality: lower is sharper and bigger. 'high' keeps the original names.
QUALITY = {'high': (16, 18), 'normal': (21, 23), 'small': (27, 29)}  # (scene loops, videos)


def cache_path(image_path, width, height, fps, fit='cover', quality='high'):
    path = Path(image_path)
    stat = path.stat()
    key = f'{VERSION}:{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{width}x{height}@{fps}'
    if fit != 'cover':
        key += f':{fit}'  # Fill-the-screen loops keep their original names.
    if is_video(path):
        key = f'video{VIDEO_VERSION}:' + key
    digest = hashlib.sha256(key.encode()).hexdigest()[:20]
    target = CACHE / f'{source_tag(path)}-{digest}.mkv'
    legacy = CACHE / f'{digest}.mkv'
    if legacy.exists() and not target.exists():
        legacy.replace(target)  # Loops rendered before names carried their source.
    return target if quality == 'high' else target.with_name(f'{target.stem}-{quality}.mkv')


def ready_loop(image_path, width, height, fps, fit='cover', quality='high'):
    """The loop in the wanted quality, else one already made in another quality.

    Changing quality never re-renders on its own (that costs minutes of CPU).
    """
    wanted = cache_path(image_path, width, height, fps, fit, quality)
    if wanted.exists():
        return wanted
    for other in QUALITY:
        candidate = cache_path(image_path, width, height, fps, fit, other)
        if candidate.exists():
            return candidate
    return None


def derived_files(image_path):
    """Loops and thumbnails PhoneCam made for this background."""
    tag = source_tag(image_path)
    return sorted(CACHE.glob(f'{tag}-*.mkv')) + sorted((CACHE / 'thumbs').glob(f'{tag}.*'))


def load(image_path):
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('Δεν διαβάστηκε η εικόνα φόντου.')
    return image


_worker_scene = None


def _init_worker(image_path, width, height, fps, info):
    global _worker_scene
    os.nice(10)  # Rendering must not starve the live camera.
    cv2.setNumThreads(1)
    _worker_scene = Scene(load(image_path), width, height, fps, info)


def _render_frame(index):
    return _worker_scene.frame(index).tobytes()


def render(image_path, width, height, fps, model=None, progress=None, cancelled=None, fit='cover', quality='high'):
    """Render the loop once and return its cached path."""
    mode = resolve_fit(image_path, fit, width, height)
    target = cache_path(image_path, width, height, fps, mode, quality)
    if target.exists():
        return target
    CACHE.mkdir(parents=True, exist_ok=True)
    # Whole-picture modes animate the picture at its own shape and frame it on encode.
    outer_width, outer_height = width, height
    if mode != 'cover':
        width, height = inner_size(image_path, outer_width, outer_height)
    info = analyse(cover(load(image_path), width, height), model)
    frames = max(1, round(fps * LOOP_SECONDS))
    temporary = target.with_suffix('.part.mkv')
    encoder = subprocess.Popen([
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{width}x{height}', '-r', str(fps), '-i', 'pipe:0',
        '-filter_complex', fit_graph(mode, outer_width, outer_height, '[0:v]', '[out]'), '-map', '[out]',
        '-c:v', 'libx264', '-preset', 'medium', '-crf', str(QUALITY[quality][0]), '-pix_fmt', 'yuv420p', '-g', str(fps * 2),
        str(temporary),
    ], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    # Few, low-priority workers: this runs while games and calls are open.
    workers = max(1, min(3, (os.cpu_count() or 2) // 4))
    try:
        with get_context('spawn').Pool(workers, _init_worker, (str(image_path), width, height, fps, info)) as pool:
            for index, data in enumerate(pool.imap(_render_frame, range(frames), chunksize=2)):
                if cancelled and cancelled():
                    raise InterruptedError
                encoder.stdin.write(data)
                if progress:
                    progress(index + 1, frames)
        encoder.stdin.close()
        if encoder.wait() != 0:
            raise RuntimeError('Αποτυχία κωδικοποίησης φόντου: ' + encoder.stderr.read().decode(errors='replace')[-300:])
        temporary.replace(target)
        return target
    finally:
        if encoder.poll() is None:
            encoder.kill()
            encoder.wait()
        temporary.unlink(missing_ok=True)


def duration(path):
    # FFmpeg prints the container duration while probing; no ffprobe needed.
    result = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(path)], capture_output=True, text=True, timeout=20)
    match = re.search(r'Duration: (\d+):(\d+):([\d.]+)', result.stderr)
    if not match:
        raise ValueError('Δεν διαβάστηκε το βίντεο φόντου.')
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def seamless(video_path, width, height, fps, progress=None, cancelled=None, fit='cover', quality='high'):
    """Make a video loop invisibly: its last moments dissolve into its beginning.

    Also converts it to the camera size and rate once, so playback costs little.
    """
    mode = resolve_fit(video_path, fit, width, height)
    target = cache_path(video_path, width, height, fps, mode, quality)
    if target.exists():
        return target
    CACHE.mkdir(parents=True, exist_ok=True)
    length = duration(video_path)
    fade = min(1.5, length / 4)
    shape = f'[0:v]fps={fps}[rate];' + fit_graph(mode, width, height, '[rate]', '[framed]') + ';[framed]format=yuv420p'
    if length >= 2:
        # Output frame t shows v(t + fade); its final `fade` seconds blend toward v(fade),
        # which is exactly where the next pass starts.
        graph = (f'{shape},split[a][b];[a]trim=start={fade},setpts=PTS-STARTPTS[body];'
                 f'[b]trim=end={fade},setpts=PTS-STARTPTS[head];'
                 f'[body][head]xfade=transition=fade:duration={fade}:offset={length - 2 * fade:.3f}[out]')
        total = length - fade
    else:
        graph = f'{shape}[out]'
        total = length
    temporary = target.with_suffix('.part.mkv')
    process = subprocess.Popen([
        'nice', '-n', '10', 'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-progress', 'pipe:1', '-nostats',
        '-i', str(video_path), '-filter_complex', graph, '-map', '[out]', '-an',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(QUALITY[quality][1]), '-g', str(fps * 2), '-threads', '3', str(temporary),
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for line in process.stdout:
            if cancelled and cancelled():
                raise InterruptedError
            if line.startswith('out_time_us=') and progress and total > 0:
                try:
                    progress(min(int(line.split('=')[1]) / 1e6, total), total)
                except ValueError:
                    pass
        if process.wait() != 0:
            raise RuntimeError(process.stderr.read()[-300:].strip() or 'αποτυχία FFmpeg')
        temporary.replace(target)
        return target
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        temporary.unlink(missing_ok=True)
