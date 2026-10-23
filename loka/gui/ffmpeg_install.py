"""Non-blocking FFmpeg install controller; workers never touch Qt widgets."""
import threading

from PySide6.QtCore import QObject, Signal

from ..ffmpeg_setup import install_ffmpeg


class FFmpegInstallController(QObject):
    busy_changed=Signal(bool)
    progress_changed=Signal(str,int,int)
    completed=Signal(object)
    _finished=Signal(object)

    def __init__(self,parent):
        super().__init__(parent)
        self.active=False;self.thread=None;self._cancel=threading.Event()
        self._finished.connect(self._on_finished)

    def start(self,proxy_url):
        if self.active:return
        self._cancel.clear();self.active=True;self.busy_changed.emit(True)
        finished=self._finished;progress=self.progress_changed;cancelled=self._cancel.is_set
        def report(phase,done,total):
            try:progress.emit(phase,min(done,2147483647),min(total,2147483647))
            except RuntimeError:self._cancel.set()
        def work():
            try:outcome=install_ffmpeg(proxy_url=proxy_url,cancelled=cancelled,on_progress=report)
            except Exception as error:outcome=error
            try:finished.emit(outcome)
            except RuntimeError:pass
        self.thread=threading.Thread(target=work,name='Loka-FFmpeg-install',daemon=True)
        self.thread.start()

    def cancel(self):self._cancel.set()

    def _on_finished(self,outcome):
        if not self.active:return
        self.active=False;self.busy_changed.emit(False);self.completed.emit(outcome)
