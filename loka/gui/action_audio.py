"""Lazy background loading and current-viewmodel-only sound playback."""
import random
import threading

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from ..action_audio import SoundBank, due_cues, load_sound_bank


class ActionAudio(QObject):
    state_changed = Signal()
    failed = Signal(str)
    _completed = Signal(object)

    def __init__(self, parent=None, *, output_factory=None):
        super().__init__(parent)
        self.output_factory = output_factory
        self.enabled = False
        self.loading = False
        self.bank = None
        self.notes = ()
        self._source = None
        self._generation = 0
        self._cancel = threading.Event()
        self._output = None
        self._closed = False
        self._loaders = {}
        self._completed.connect(self._accept)
        self._cleanup = QTimer(self)
        self._cleanup.setInterval(25)
        self._cleanup.timeout.connect(self._collect)
        if parent is not None:
            parent.destroyed.connect(self.close)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.close)

    def set_source(self, session, previews):
        self.stop()
        self._cancel.set()
        self._cancel = threading.Event()
        self._generation += 1
        self._source = (session, tuple(previews)) if session else None
        self.bank = None
        self.notes = ()
        self.loading = False
        if self.enabled:
            self._load()
        self.state_changed.emit()

    def detach_source(self):
        """GUI stop/detach; return plain CPU owners and threads for worker disposal."""
        owners = [self._source, self.bank, self.notes]
        loaders = list(self._loaders.values())
        self._loaders.clear()
        self.set_source(None, ())
        return owners, loaders

    def set_enabled(self, enabled):
        self.stop()
        self.enabled = bool(enabled)
        if self.enabled:
            self._load()
        self.state_changed.emit()

    def _load(self):
        if self._closed or not self._source or self.loading or self.bank is not None:
            return
        source, generation, cancel = self._source, self._generation, self._cancel
        self.loading = True
        result = []
        def worker():
            nonlocal source
            try:
                bank = load_sound_bank(*source, cancelled=cancel.is_set)
            except InterruptedError:
                source = None
                return
            except Exception as error:
                bank = SoundBank({}, {}, ('音效载入失败：'+str(error),))
            source = None
            if not cancel.is_set():
                result.append(bank)
                try:
                    self._completed.emit(generation)
                except RuntimeError:
                    pass  # QObject was destroyed while reading files.
        thread = threading.Thread(target=worker, name='Loka sound preview', daemon=True)
        self._loaders[generation] = (thread, result)
        thread.start()

    def _accept(self, generation):
        loader = self._loaders.get(generation)
        if loader is None:
            return
        if self._closed or generation != self._generation:
            # Keep the thread tracked until a later unload can join it.
            loader[1].clear()
            return
        if not loader[1]:
            return
        bank = loader[1].pop()
        self.bank, self.notes, self.loading = bank, bank.notes, False
        self.state_changed.emit()

    def advance(self, model_index, sequence_index, previous, current, duration, *, loop=False, include_start=False):
        if not self.enabled or self.bank is None or self._closed:
            return
        cues = self.bank.cues.get((model_index, sequence_index), ())
        for cue in due_cues(cues, previous, current, duration, loop=loop, include_start=include_start):
            clips = self.bank.clips.get((model_index, cue.name), ())
            if not clips:
                continue
            try:
                if self._output is None:
                    if self.output_factory is None:
                        from ..action_waveout import WaveOutput
                        self._output = WaveOutput()
                    else:
                        self._output = self.output_factory()
                self._output.play(random.choice(clips))
                self._cleanup.start()
            except OSError as error:
                self.set_enabled(False)
                self.failed.emit('音效预览不可用：'+str(error))
                break

    def _collect(self):
        if self._output is not None:
            self._output.collect()
            if not getattr(self._output, 'voices', ()):
                self._cleanup.stop()

    def stop(self):
        self._cleanup.stop()
        if self._output is not None:
            self._output.stop()

    def close(self, *_):
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        self.stop()
        if self._output is not None:
            self._output.close()
