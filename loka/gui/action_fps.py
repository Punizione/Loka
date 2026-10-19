"""FPS editing: accept temporary text, normalize invalid commits to 30 FPS."""
import math

from PySide6.QtGui import QValidator
from qfluentwidgets import DoubleSpinBox

from ..fps_values import round_fps


class ActionFpsInput(DoubleSpinBox):
    DEFAULT_FPS=30.
    MIN_FPS=.01
    MAX_FPS=100000.

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setDecimals(2);self.setRange(self.MIN_FPS,self.MAX_FPS)
        self.setSingleStep(1);self.setKeyboardTracking(False)
        self.setValue(self.DEFAULT_FPS)

    def _normalized(self,value):
        try:
            if isinstance(value,bool):return self.DEFAULT_FPS
            rate=float(value)
            if not math.isfinite(rate) or not self.MIN_FPS<=rate<=self.MAX_FPS:return self.DEFAULT_FPS
            return round_fps(rate)
        except (ValueError,TypeError,OverflowError):return self.DEFAULT_FPS

    def _parse(self,text):
        rate,ok=self.locale().toDouble(str(text).strip())
        return self._normalized(rate) if ok else self.DEFAULT_FPS

    def validate(self,text,position):
        raw=str(text).strip()
        value,ok=self.locale().toDouble(raw)
        if ok and (not math.isfinite(value) or not self.MIN_FPS<=value<=self.MAX_FPS):
            # Let an invalid number reach valueFromText instead of Qt silently
            # clamping it to 0.01 or rejecting just its minus sign.
            return QValidator.Acceptable,text,position
        state,_,_=super().validate(text,position)
        return (QValidator.Intermediate if state==QValidator.Invalid else state),text,position

    def valueFromText(self,text):
        return self._parse(text)

    def fixup(self,text):
        return self.textFromValue(self._parse(text))

    def setValue(self,value):
        super().setValue(self._normalized(value))

    @property
    def has_pending_text(self):
        return self.lineEdit().text().strip()!=self.textFromValue(self.value())
