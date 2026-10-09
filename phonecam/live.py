"""FFmpeg filter commands through the system libzmq, without extra Python deps."""
import ctypes
import ctypes.util
from pathlib import Path
import queue
from PySide6.QtCore import QThread, Signal


class LiveControls(QThread):
    applied = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, endpoint, parent=None):
        super().__init__(parent)
        self.endpoint = endpoint.encode()
        self.messages = queue.Queue()

    def submit(self, generation, capture, commands):
        self.messages.put((generation, capture, commands))

    def close(self):
        self.requestInterruption()
        self.messages.put(None)
        self.wait(1800)

    def run(self):
        context = None
        lib = None
        try:
            lib = ctypes.CDLL(zmq_library())
            signatures = {
                'zmq_ctx_new': (ctypes.c_void_p, []),
                'zmq_ctx_term': (ctypes.c_int, [ctypes.c_void_p]),
                'zmq_socket': (ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_int]),
                'zmq_close': (ctypes.c_int, [ctypes.c_void_p]),
                'zmq_connect': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p]),
                'zmq_setsockopt': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]),
                'zmq_send': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]),
                'zmq_recv': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]),
            }
            for name, (result, arguments) in signatures.items():
                method = getattr(lib, name)
                method.restype, method.argtypes = result, arguments
            context = lib.zmq_ctx_new()
            if not context:
                raise RuntimeError('Δεν ξεκίνησε ο έλεγχος φίλτρων.')
            while not self.isInterruptionRequested():
                message = self.messages.get()
                if message is None:
                    break
                # Coalesce slider changes before touching the active stream.
                while not self.messages.empty():
                    message = self.messages.get()
                    if message is None:
                        return
                generation, capture, commands = message
                client = lib.zmq_socket(context, 3)  # ZMQ_REQ
                if not client:
                    self.failed.emit(generation, 'Δεν άνοιξε ο έλεγχος φίλτρων.')
                    continue
                try:
                    for option, value in ((17, 0), (27, 600), (28, 300)):  # linger, receive/send timeout
                        number = ctypes.c_int(value)
                        lib.zmq_setsockopt(client, option, ctypes.byref(number), ctypes.sizeof(number))
                    if lib.zmq_connect(client, self.endpoint):
                        raise RuntimeError('Δεν συνδέθηκε ο έλεγχος φίλτρων.')
                    for command in commands:
                        if self.isInterruptionRequested():
                            return
                        data = command.encode()
                        if lib.zmq_send(client, data, len(data), 0) < 0:
                            raise RuntimeError('Η αλλαγή εικόνας καθυστέρησε.')
                        response = ctypes.create_string_buffer(2048)
                        size = lib.zmq_recv(client, response, 2048, 0)
                        if size < 0:
                            raise RuntimeError('Η αλλαγή εικόνας καθυστέρησε.')
                        reply = response.raw[:size].decode(errors='replace')
                        if not reply.startswith('0 '):
                            raise RuntimeError('Αποτυχία φίλτρου: ' + reply[:160])
                    self.applied.emit(generation, capture)
                except (OSError, RuntimeError) as exc:
                    self.failed.emit(generation, str(exc))
                finally:
                    lib.zmq_close(client)
        except (OSError, RuntimeError) as exc:
            self.failed.emit(-1, str(exc))
        finally:
            if context and lib:
                lib.zmq_ctx_term(context)


def zmq_library():
    found = ctypes.util.find_library('zmq')
    if found:
        return found
    # The AppImage carries libzmq inside the pyzmq wheel.
    try:
        import zmq
        bundled = sorted(Path(zmq.__file__).resolve().parents[1].glob('pyzmq.libs/libzmq*.so*'))
        if bundled:
            return str(bundled[0])
    except ImportError:
        pass
    return 'libzmq.so.5'


def fit_width(capture):
    width, height = map(int, capture.size.split('x'))
    if capture.rotation in (90, 270):
        return max(2, int(width * min(height / width, width / height)) // 2 * 2)
    return width


def commands(capture):
    saturation = {'natural': 1, 'warm': 1.05, 'mono': 0, 'vivid': 1.35}[capture.look]
    return [
        f'eq@tone brightness {capture.exposure / 100}',
        f'eq@tone contrast {1.08 if capture.look == "vivid" else 1}',
        f'eq@tone saturation {saturation}',
        f'colorbalance@warmth rs {0.08 if capture.look == "warm" else 0}',
        f'colorbalance@warmth bs {-0.06 if capture.look == "warm" else 0}',
        f'scale@fit w {fit_width(capture)}',
        f'rotate@turn angle {capture.rotation}*PI/180',
        f'hflip@mirror enable {int(capture.mirror)}',
    ]
