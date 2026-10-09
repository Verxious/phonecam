"""Model files: the person matte ships with the app, the scene model is fetched once."""
import hashlib
from pathlib import Path
import urllib.request
from .config import STATE

ROOT = Path(__file__).resolve().parent
PERSON = ROOT / 'assets' / 'selfie_segmenter_landscape.tflite'
SCENE = STATE / 'models' / 'segformer-b2-ade-fp16.onnx'
SCENE_URL = 'https://huggingface.co/Xenova/segformer-b2-finetuned-ade-512-512/resolve/df795789e70f4089c8658907679c6fd2367c89a5/onnx/model_fp16.onnx'
SCENE_SHA256 = '79209c2663c66b35af907b9bfeab79570d396ab7c4d0c22a54814288538a8d1b'


def digest(path):
    hasher = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            hasher.update(block)
    return hasher.hexdigest()


def scene_model(progress=None, cancelled=None):
    """Return the verified scene model path, downloading it if needed."""
    if SCENE.exists():
        return SCENE
    SCENE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SCENE.with_suffix('.part')
    try:
        with urllib.request.urlopen(SCENE_URL, timeout=30) as response, open(temporary, 'wb') as handle:
            total = int(response.headers.get('Content-Length') or 0)
            done = 0
            while block := response.read(1 << 20):
                if cancelled and cancelled():
                    raise InterruptedError
                handle.write(block)
                done += len(block)
                if progress:
                    progress(done, total)
        if digest(temporary) != SCENE_SHA256:
            raise ValueError('Το μοντέλο σκηνής δεν επαληθεύτηκε.')
        temporary.replace(SCENE)
        return SCENE
    finally:
        temporary.unlink(missing_ok=True)
