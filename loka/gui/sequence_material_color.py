"""RGB sweep palette with an explicit return to the authored recipe default."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QColorDialog, QDialog, QHBoxLayout, QVBoxLayout
from qfluentwidgets import CaptionLabel, PrimaryPushButton, PushButton

from ..i18n import tr


class SweepColorDialog(QDialog):
    def __init__(self, default_color, current=None, parent=None, *,
                 title='喷头色彩', custom_label='自定义喷头色彩'):
        super().__init__(parent)
        self.setWindowTitle(tr(title))
        self.custom_label = custom_label
        self.default_color = tuple(default_color)
        self.use_default = current is None or tuple(current) == self.default_color
        self.selected_color = None
        layout = QVBoxLayout(self)
        self.caption = CaptionLabel(self)
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.color_picker = QColorDialog(QColor(*(current or default_color)), self)
        self.color_picker.setWindowFlags(Qt.Widget)
        self.color_picker.setOptions(QColorDialog.DontUseNativeDialog | QColorDialog.NoButtons)
        layout.addWidget(self.color_picker)
        row = QHBoxLayout()
        restore = PushButton(tr('恢复配方默认'), self)
        restore.clicked.connect(self._restore)
        row.addWidget(restore)
        row.addStretch(1)
        confirm = PrimaryPushButton(tr('确定'), self)
        confirm.clicked.connect(self.accept)
        row.addWidget(confirm)
        cancel = PushButton(tr('取消'), self)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        layout.addLayout(row)
        self.color_picker.currentColorChanged.connect(self._changed)
        self._update_caption()

    def _rgb(self):
        value = self.color_picker.currentColor()
        return value.red(), value.green(), value.blue()

    def _changed(self, *_):
        self.use_default = self._rgb() == self.default_color
        self._update_caption()

    def _restore(self):
        self.color_picker.setCurrentColor(QColor(*self.default_color))
        self.use_default = True
        self._update_caption()

    def _update_caption(self):
        color = self._rgb()
        mode = tr('配方默认') if self.use_default else tr(self.custom_label)
        self.caption.setText(tr('{mode} · RGB({r}, {g}, {b})', mode=mode, r=color[0], g=color[1], b=color[2]))

    def accept(self):
        self.selected_color = None if self.use_default else self._rgb()
        super().accept()
