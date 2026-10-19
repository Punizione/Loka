"""Small asynchronous Windows waveform output; no QtMultimedia/codec payload."""
import ctypes as C
import sys


class _WaveHeader(C.Structure):
    _fields_ = [('data', C.c_void_p), ('length', C.c_uint32),
                ('recorded', C.c_uint32), ('user', C.c_size_t),
                ('flags', C.c_uint32), ('loops', C.c_uint32),
                ('next', C.c_void_p), ('reserved', C.c_size_t)]


class _Voice:
    def __init__(self, api, clip):
        self.api = api
        self.handle = C.c_void_p()
        self.buffer = C.create_string_buffer(clip.samples)
        self.header = _WaveHeader(C.addressof(self.buffer), len(clip.samples), 0, 0, 0, 0, None, 0)
        self.prepared = False
        fmt = C.create_string_buffer(clip.format)
        _check(api.waveOutOpen(C.byref(self.handle), 0xffffffff, fmt, 0, 0, 0), '打开音频设备')

    def start(self):
        _check(self.api.waveOutPrepareHeader(self.handle, C.byref(self.header), C.sizeof(self.header)), '准备音频缓冲')
        self.prepared = True
        _check(self.api.waveOutWrite(self.handle, C.byref(self.header), C.sizeof(self.header)), '播放音频')

    def release(self, reset=False):
        if not self.handle.value:
            return True
        if reset and self.api.waveOutReset(self.handle):
            return False
        if self.prepared:
            if self.api.waveOutUnprepareHeader(self.handle, C.byref(self.header), C.sizeof(self.header)):
                return False
            self.prepared = False
        if self.api.waveOutClose(self.handle):
            return False
        self.handle = C.c_void_p()
        return True


def _check(result, operation):
    if result:
        raise OSError(f'{operation}失败（Windows 音频错误 {result}）')


# In the exceptional case of a driver refusing reset/unprepare, keep buffers
# alive rather than freeing memory still owned by that driver.
_retained_voices = []


class WaveOutput:
    def __init__(self):
        if sys.platform != 'win32':
            raise OSError('音效预览需要 Windows 音频输出')
        self.api = C.WinDLL('winmm')
        self.api.waveOutOpen.argtypes = (C.POINTER(C.c_void_p), C.c_uint32, C.c_void_p, C.c_size_t, C.c_size_t, C.c_uint32)
        for name in ('waveOutPrepareHeader', 'waveOutWrite', 'waveOutUnprepareHeader'):
            getattr(self.api, name).argtypes = (C.c_void_p, C.POINTER(_WaveHeader), C.c_uint32)
        for name in ('waveOutReset', 'waveOutClose'):
            getattr(self.api, name).argtypes = (C.c_void_p,)
        for name in ('waveOutOpen', 'waveOutPrepareHeader', 'waveOutWrite',
                     'waveOutUnprepareHeader', 'waveOutReset', 'waveOutClose'):
            getattr(self.api, name).restype = C.c_uint32
        self.voices = []

    def play(self, clip):
        self.collect()
        if len(self.voices) >= 32:
            oldest = self.voices[0]
            if not oldest.release(reset=True):
                raise OSError('音频输出繁忙')
            self.voices.pop(0)
        voice = _Voice(self.api, clip)
        self.voices.append(voice)
        try:
            voice.start()
        except OSError:
            if voice.release(reset=True):
                self.voices.remove(voice)
            raise

    def collect(self):
        self.voices[:] = [voice for voice in self.voices
                          if not (voice.header.flags & 1 and voice.release())]

    def stop(self):
        self.voices[:] = [voice for voice in self.voices if not voice.release(reset=True)]

    def close(self):
        self.stop()
        _retained_voices.extend(self.voices)
        self.voices.clear()
