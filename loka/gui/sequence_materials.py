"""Advanced single-material workflow and read-only 2D product preview."""
from __future__ import annotations

import math
from pathlib import Path
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal, QUrl, QRectF
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHeaderView, QHBoxLayout,
                               QDialog, QSizePolicy, QStackedWidget, QTableWidgetItem, QVBoxLayout, QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, CardWidget, ComboBox,
                           LineEdit, Pivot, PrimaryPushButton, ProgressBar,
                           PushButton, ScrollArea, Slider, SubtitleLabel,
                           TableWidget, TextEdit, isDarkTheme)

from ..i18n import tr, display_text
from ..process_limits import exception_text
from ..sequence_material_recipes import BUILTIN_RECIPES
from ..sequence_materials import (default_output_directory, generate_material,
                                generate_all_materials, output_path, save_material, scan_material_directory)


class MaterialViewport(QWidget):
    """Draw only decoded product pixels. RGB ignores alpha; Alpha is grayscale."""

    def __init__(self, parent=None, *, placeholder='选择 VTF 后点击“加载预览”'):
        super().__init__(parent)
        self.setMinimumHeight(390)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image = QImage()
        self.placeholder = placeholder

    def set_frame(self, frame, channel):
        if channel == 'alpha':
            alpha = frame.rgba[3::4]
            self.image = QImage(alpha, frame.width, frame.height, frame.width,
                                QImage.Format_Grayscale8).copy()
        else:
            self.image = QImage(frame.rgba, frame.width, frame.height, frame.width * 4,
                                QImage.Format_RGBA8888).convertToFormat(QImage.Format_RGB888)
        self.update()

    def clear(self):
        self.image = QImage()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        area = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        painter.setBrush(QColor('#232323' if isDarkTheme() else '#fafafa'))
        painter.setPen(QPen(QColor('#424242' if isDarkTheme() else '#dddddd')))
        painter.drawRoundedRect(area, 5, 5)
        if self.image.isNull():
            painter.setPen(QColor('#bbbbbb' if isDarkTheme() else '#616161'))
            painter.drawText(self.rect().adjusted(16, 16, -16, -16),
                             Qt.AlignCenter | Qt.TextWordWrap, tr(self.placeholder))
        else:
            available = self.rect().adjusted(10, 10, -10, -10)
            size = self.image.size().scaled(available.size(), Qt.KeepAspectRatio)
            target = QRectF(0, 0, size.width(), size.height())
            target.moveCenter(QRectF(available).center())
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawImage(target, self.image)


class _Signals(QObject):
    progress = Signal(str, int, int)
    finished = Signal(str, object, object)
    batch_item = Signal(int, str)


class SequenceMaterialInterface(ScrollArea):
    busy_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('sequenceMaterialInterface')
        self.active = False
        self._thread = None
        self._cancel = threading.Event()
        self._signals = _Signals(self)
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)
        self._signals.batch_item.connect(self._on_batch_item)
        self._task_kind = ''
        self._batch_index = 0
        self._batch_fraction = 0.
        self._sweep_colors = {}
        self._default_sweep_color = None
        self._wire_colors = {}
        self._default_wire_color = None
        self._scan = None
        self._cached = None
        self._preview_loaded = False
        self._last_output = None
        self._frame = 0
        self._channel = 'rgb'
        self._playing = False
        self._play_started = 0.
        self._play_frame = 0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

        self.scrollWidget = QWidget(self)
        layout = QVBoxLayout(self.scrollWidget)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        layout.addWidget(SubtitleLabel(tr('序列帧材质处理'), self.scrollWidget))

        recipe_card, recipe_layout = self._card(layout, '配方')
        row = QHBoxLayout()
        self.recipe_box = ComboBox(recipe_card)
        self.recipe_box.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        for recipe in BUILTIN_RECIPES:
            self.recipe_box.addItem(recipe.name, userData=recipe.identifier)
        row.addWidget(self.recipe_box, 1)
        self.import_button = PushButton(tr('导入配方'), recipe_card)
        self.import_button.setEnabled(False)
        self.import_button.setToolTip(tr('配方导入暂未开放'))
        row.addWidget(self.import_button)
        recipe_layout.addLayout(row)

        paths_card, paths_layout = self._card(layout, '输入 / 输出')
        self.input_edit, self.input_browse, input_row = self._path_row(
            paths_card, paths_layout, '输入目录(materials/models)')
        self.scan_button = PushButton(tr('扫描目录'), paths_card)
        input_row.addWidget(self.scan_button)
        self.output_edit, self.output_browse, _ = self._path_row(
            paths_card, paths_layout, '输出目录')
        self.output_edit.setPlaceholderText(tr('留空：在输入贴图包旁新建“包名_序列帧”目录'))

        mode_card, mode_layout = self._card(layout)
        self.mode_tabs = Pivot(mode_card)
        self.mode_stack = QStackedWidget(mode_card)
        self.batch_mode = QWidget(self.mode_stack)
        self.material_mode = QWidget(self.mode_stack)
        self.mode_stack.addWidget(self.batch_mode)
        self.mode_stack.addWidget(self.material_mode)
        self.mode_tabs.addItem('batch', tr('一键处理'), onClick=lambda: self._set_mode(0))
        self.mode_tabs.addItem('material', tr('材质模式'), onClick=lambda: self._set_mode(1))
        self.mode_tabs.setCurrentItem('batch')
        mode_layout.addWidget(self.mode_tabs)
        mode_layout.addWidget(self.mode_stack)
        batch_layout = QVBoxLayout(self.batch_mode)
        batch_layout.setContentsMargins(0, 8, 0, 0)
        self.batch_summary = CaptionLabel(tr('扫描目录后显示全部可处理清单。'), self.batch_mode)
        self.batch_summary.setWordWrap(True)
        batch_layout.addWidget(self.batch_summary)
        self.batch_table = TableWidget(self.batch_mode)
        self.batch_table.setColumnCount(4)
        self.batch_table.setHorizontalHeaderLabels([tr('部件'), tr('输出层'), tr('原始文件名称'), tr('状态')])
        self.batch_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.batch_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.batch_table.verticalHeader().hide()
        self.batch_table.setMinimumHeight(280)
        header = self.batch_table.horizontalHeader()
        for column in (0, 1, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        batch_layout.addWidget(self.batch_table)
        material_layout = QVBoxLayout(self.material_mode)
        material_layout.setContentsMargins(0, 8, 0, 0)
        material_layout.setSpacing(8)
        material_layout.addWidget(CaptionLabel(tr('选择 VTF'), self.material_mode))
        row = QHBoxLayout()
        self.texture_box = ComboBox(self.material_mode)
        self.texture_box.setMinimumWidth(0)
        self.texture_box.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        row.addWidget(self.texture_box, 1)
        self.wire_color_button = PushButton(tr('线框色彩'), self.material_mode)
        row.addWidget(self.wire_color_button)
        self.color_button = PushButton(tr('喷头色彩'), self.material_mode)
        row.addWidget(self.color_button)
        self.load_button = PushButton(tr('加载预览'), self.material_mode)
        row.addWidget(self.load_button)
        material_layout.addLayout(row)
        self.viewport = MaterialViewport(self.material_mode)
        material_layout.addWidget(self.viewport, 1)
        row = QHBoxLayout()
        self.play_button = PushButton(tr('播放'), self.material_mode)
        row.addWidget(self.play_button)
        row.addWidget(CaptionLabel(tr('30 FPS · 固定'), self.material_mode))
        self.resolution_label = CaptionLabel(tr('目标分辨率：—'), self.material_mode)
        row.addWidget(self.resolution_label)
        row.addStretch(1)
        self.rgb_button = PushButton('RGB', self.material_mode)
        self.alpha_button = PushButton('Alpha', self.material_mode)
        for button in (self.rgb_button, self.alpha_button):
            button.setCheckable(True)
            row.addWidget(button)
        self.rgb_button.setChecked(True)
        material_layout.addLayout(row)
        row = QHBoxLayout()
        self.frame_slider = Slider(Qt.Horizontal, self.material_mode)
        self.frame_slider.setRange(0, 0)
        row.addWidget(self.frame_slider, 1)
        self.frame_label = CaptionLabel(tr('— / — 帧'), self.material_mode)
        row.addWidget(self.frame_label)
        material_layout.addLayout(row)
        self.spec_label = CaptionLabel(tr('尚未加载预览'), self.material_mode)
        self.spec_label.setWordWrap(True)
        material_layout.addWidget(self.spec_label)

        self.progress = ProgressBar(self.scrollWidget)
        self.progress.setRange(0, 100)
        self.progress.hide()
        layout.addWidget(self.progress)
        actions = QHBoxLayout()
        self.status_label = CaptionLabel(tr('请选择输入目录并扫描。'), self.scrollWidget)
        self.status_label.setWordWrap(True)
        actions.addWidget(self.status_label, 1)
        self.open_button = PushButton(tr('打开输出目录'), self.scrollWidget)
        actions.addWidget(self.open_button)
        self.run_button = PrimaryPushButton(tr('开始生成'), self.scrollWidget)
        actions.addWidget(self.run_button)
        layout.addLayout(actions)
        self.log_button = PushButton(tr('处理日志'), self.scrollWidget)
        self.log_button.setCheckable(True)
        layout.addWidget(self.log_button, 0, Qt.AlignLeft)
        self.log = TextEdit(self.scrollWidget)
        self.log.setReadOnly(True)
        self.log.setFixedHeight(160)
        self.log.document().setMaximumBlockCount(500)
        self.log.hide()
        self.log_button.toggled.connect(self.log.setVisible)
        layout.addWidget(self.log)
        layout.addStretch(1)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.enableTransparentBackground()

        self.input_browse.clicked.connect(self._browse_input)
        self.output_browse.clicked.connect(self._browse_output)
        self.input_edit.textChanged.connect(self._invalidate_scan)
        self.recipe_box.currentIndexChanged.connect(self._invalidate_scan)
        self.output_edit.textChanged.connect(self._output_changed)
        self.scan_button.clicked.connect(self.scan_directory)
        self.texture_box.currentIndexChanged.connect(self._selection_changed)
        self.load_button.clicked.connect(self.load_preview)
        self.color_button.clicked.connect(self._choose_sweep_color)
        self.wire_color_button.clicked.connect(self._choose_wire_color)
        self.play_button.clicked.connect(self._toggle_play)
        self.rgb_button.clicked.connect(lambda: self._set_channel('rgb'))
        self.alpha_button.clicked.connect(lambda: self._set_channel('alpha'))
        self.frame_slider.valueChanged.connect(self._seek)
        self.frame_slider.sliderPressed.connect(self.pause)
        self.run_button.clicked.connect(self._run_or_cancel)
        self.open_button.clicked.connect(self._open_output)
        self._refresh_buttons()

    def _card(self, layout, title=None):
        card = CardWidget(self.scrollWidget)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(18, 14, 18, 14)
        inner.setSpacing(8)
        if title:
            inner.addWidget(BodyLabel(tr(title), card))
        layout.addWidget(card)
        return card, inner

    def _path_row(self, card, layout, label):
        caption = CaptionLabel(tr(label), card)
        caption.setWordWrap(True)
        layout.addWidget(caption)
        row = QHBoxLayout()
        edit = LineEdit(card)
        row.addWidget(edit, 1)
        button = PushButton(tr('浏览'), card)
        row.addWidget(button)
        layout.addLayout(row)
        return edit, button, row

    def _append(self, message):
        self.log.append(display_text(message))
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _status(self, source, **values):
        message = tr(source, **values)
        self.status_label.setText(message)
        return message

    def _choice(self):
        return self.texture_box.currentData() if self._scan is not None else None

    def set_input_path(self, path):
        if self.active:
            return
        self.input_edit.setText(str(path))

    def _browse_input(self):
        path = QFileDialog.getExistingDirectory(self, tr('选择材质输入目录'), self.input_edit.text())
        if path:
            self.set_input_path(path)

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(self, tr('选择输出目录'), self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def _output_changed(self, *_):
        self._last_output = None
        self._refresh_buttons()

    def _invalidate_scan(self, *_):
        if self.active:
            self.cancel()
        self._scan = None
        self._sweep_colors.clear()
        self._wire_colors.clear()
        self._populate_batch()
        self.texture_box.clear()
        self._clear_preview()
        self._last_output = None
        self._status('输入或配方已更改，请扫描目录。')
        self._refresh_buttons()

    def _clear_preview(self):
        self.pause()
        self._cached = None
        self._preview_loaded = False
        self._frame = 0
        self.viewport.clear()
        self.frame_slider.blockSignals(True)
        self.frame_slider.setRange(0, 0)
        self.frame_slider.blockSignals(False)
        self.frame_label.setText(tr('— / — 帧'))
        self.spec_label.setText(tr('尚未加载预览'))

    def _selection_changed(self, *_):
        self._clear_preview()
        self._last_output = None
        choice = self._choice()
        self.texture_box.setToolTip(str(choice.source) + '\n' + str(choice.relative_output) if choice else '')
        self._refresh_color()
        if choice:
            self._status('已选择 VTF，可加载预览或直接生成。')
        self._refresh_buttons()

    def _refresh_color(self):
        from ..sequence_material_templates import default_sweep_color, default_wire_color
        choice = self._choice()
        self._default_sweep_color = self._refresh_palette_button(
            choice, self.color_button, self._sweep_colors, default_sweep_color,
            '自定义喷头色彩', '该项参考动画没有可提取的打印前沿，暂不支持调色。')
        self._default_wire_color = self._refresh_palette_button(
            choice, self.wire_color_button, self._wire_colors, default_wire_color,
            '自定义线框色彩', '该项参考动画没有可提取的线框，暂不支持线框调色。')

    def _refresh_palette_button(self, choice, button, colors, read_default, custom_label, unavailable):
        button.setIcon(QIcon())
        button.setToolTip('')
        if choice is None:
            return None
        if choice.rule.layer == 'normal':
            button.setToolTip(tr('法线 RGB 表示方向，不能自定义颜色。'))
            return None
        try:
            default_color = read_default(choice.recipe_id, choice.rule)
        except (OSError, ValueError) as error:
            button.setToolTip(display_text(exception_text(error)))
            return None
        if default_color is None:
            button.setToolTip(tr(unavailable))
            return None
        color = colors.get(choice, default_color)
        swatch = QPixmap(16, 16)
        swatch.fill(QColor(*color))
        button.setIcon(QIcon(swatch))
        mode = tr(custom_label) if choice in colors else tr('配方默认')
        button.setToolTip(tr('{mode} · RGB({r}, {g}, {b})', mode=mode, r=color[0], g=color[1], b=color[2]))
        return default_color

    def _choose_sweep_color(self):
        self._choose_palette(self._default_sweep_color, self._sweep_colors, '喷头色彩', '自定义喷头色彩')

    def _choose_wire_color(self):
        self._choose_palette(self._default_wire_color, self._wire_colors, '线框色彩', '自定义线框色彩')

    def _choose_palette(self, default_color, colors, title, custom_label):
        if self.active or default_color is None:
            return
        choice = self._choice()
        if choice is None or choice.rule.layer == 'normal':
            return
        from .sequence_material_color import SweepColorDialog
        dialog = SweepColorDialog(default_color, colors.get(choice), self.window(),
                                  title=title, custom_label=custom_label)
        self.pause()
        if dialog.exec() != QDialog.Accepted:
            return
        before = colors.get(choice)
        if dialog.selected_color is None:
            colors.pop(choice, None)
        else:
            colors[choice] = dialog.selected_color
        if before != colors.get(choice):
            self._clear_preview()
            self._last_output = None
            self._status('色彩已更改，请重新加载预览或生成。')
        self._refresh_color()
        self._refresh_buttons()

    def _set_mode(self, index):
        self.pause()
        self.mode_stack.setCurrentIndex(index)
        self.mode_tabs.setCurrentItem(('batch', 'material')[index])
        self._refresh_buttons()

    def _refresh_buttons(self):
        choice = self._choice()
        self.resolution_label.setText(tr('目标分辨率：{width} × {height}', width=choice.rule.size, height=choice.rule.size)
                                      if choice is not None else tr('目标分辨率：—'))
        material = self.mode_stack.currentIndex() == 1
        batch = self.mode_stack.currentIndex() == 0
        for widget in (self.input_edit, self.input_browse, self.output_edit,
                       self.output_browse, self.recipe_box, self.scan_button, self.mode_tabs):
            widget.setEnabled(not self.active)
        self.scan_button.setEnabled(not self.active and bool(self.input_edit.text().strip()))
        self.texture_box.setEnabled(not self.active and choice is not None)
        self.load_button.setEnabled(not self.active and material and choice is not None)
        self.color_button.setEnabled(not self.active and material and choice is not None
                                     and choice.rule.layer != 'normal' and self._default_sweep_color is not None)
        self.wire_color_button.setEnabled(not self.active and material and choice is not None
                                          and choice.rule.layer != 'normal' and self._default_wire_color is not None)
        preview = not self.active and material and self._preview_loaded and self._cached is not None
        for widget in (self.play_button, self.rgb_button, self.alpha_button, self.frame_slider):
            widget.setEnabled(preview)
        self.run_button.setText(tr('取消') if self.active else tr('一键生成') if batch else tr('开始生成'))
        self.run_button.setEnabled((self.active and not self._cancel.is_set()) or
                                   (not self.active and (material or batch) and choice is not None))
        self.open_button.setEnabled(not self.active and self._last_output is not None)

    def _launch(self, kind, work):
        if self.active:
            return
        window = self.window()
        if hasattr(window, '_worker_busy') and window._worker_busy():
            self._status('请先等待当前任务完成。')
            self._refresh_buttons()
            return
        self.pause()
        self._cancel.clear()
        self._task_kind = kind
        self.active = True
        self.progress.setValue(0)
        self.progress.show()
        self._refresh_buttons()
        self.busy_changed.emit(True)
        self._status('任务处理中…')
        signals = self._signals

        def report(stage, done, total):
            signals.progress.emit(stage, min(done, 2147483647), min(total, 2147483647))

        def run():
            result = error = None
            try:
                result = work(self._cancel.is_set, report)
            except Exception as failure:
                error = failure
            try:
                signals.finished.emit(kind, result, error)
            except RuntimeError:
                pass

        self._thread = threading.Thread(target=run, name='Loka-sequence-material-' + kind, daemon=True)
        self._thread.start()

    def scan_directory(self):
        text = self.input_edit.text().strip()
        if not text or self.active:
            return
        recipe_id = self.recipe_box.currentData()
        self._scan = None
        self._populate_batch()
        self.texture_box.clear()
        self._clear_preview()
        self._last_output = None
        self._launch('scan', lambda cancelled, progress: scan_material_directory(
            text, recipe_id, cancelled=cancelled, on_progress=progress))

    def load_preview(self):
        choice = self._choice()
        if choice is None or self.active:
            return
        cached = self._cached
        color = self._sweep_colors.get(choice)
        wire_color = self._wire_colors.get(choice)
        # An externally edited/broken source must not leave an earlier image
        # appearing to be the newly requested preview after a load failure.
        self._clear_preview()
        self._launch('preview', lambda cancelled, progress: generate_material(
            choice, cached=cached, sweep_color=color, wire_color=wire_color, cancelled=cancelled, on_progress=progress))

    def _run_or_cancel(self):
        if self.active:
            self.cancel()
            return
        if self.mode_stack.currentIndex() == 0:
            self._run_batch()
            return
        choice = self._choice()
        if choice is None or self.mode_stack.currentIndex() != 1:
            return
        try:
            root = Path(self.output_edit.text().strip()).expanduser() if self.output_edit.text().strip() else default_output_directory(self._scan.root)
            target = output_path(choice, root)
            if target.exists():
                self._append(self._status('输出文件已存在，已跳过：{path}', path=str(target)))
                self._last_output = target
                self._refresh_buttons()
                return
        except (OSError, ValueError) as error:
            self._append(self._status('生成未开始：{error}', error=display_text(exception_text(error))))
            return
        cached = self._cached
        color = self._sweep_colors.get(choice)
        wire_color = self._wire_colors.get(choice)

        def work(cancelled, progress):
            product = generate_material(choice, cached=cached, sweep_color=color, wire_color=wire_color,
                                        cancelled=cancelled, on_progress=progress)
            progress('写入产物', 0, 1)
            path = save_material(product, root, cancelled=cancelled)
            return product, path

        self._launch('generate', work)

    def _populate_batch(self):
        choices = self._scan.choices if self._scan is not None else ()
        self.batch_table.setRowCount(len(choices))
        for row, choice in enumerate(choices):
            values = (choice.rule.part, tr(choice.rule.layer_label), choice.source.name, tr('就绪'))
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(str(choice.source) + '\n→ ' + str(choice.relative_output))
                self.batch_table.setItem(row, column, item)
        self.batch_summary.setText(tr('共 {count} 个可处理项', count=len(choices))
                                   if choices else tr('扫描目录后显示全部可处理清单。'))

    def _run_batch(self):
        if self._scan is None or not self._scan.choices:
            return
        try:
            root = Path(self.output_edit.text().strip()).expanduser().resolve() if self.output_edit.text().strip() else default_output_directory(self._scan.root)
        except (OSError, ValueError) as error:
            self._append(self._status('生成未开始：{error}', error=display_text(exception_text(error))))
            return
        choices = self._scan.choices
        self._populate_batch()
        self._batch_index = 0
        self._batch_fraction = 0.
        self._last_output = None
        self._launch('batch', lambda cancelled, progress: generate_all_materials(
            choices, root, cancelled=cancelled, on_progress=progress,
            on_item=self._signals.batch_item.emit))

    def _on_batch_item(self, index, state):
        if index != self._batch_index:
            self._batch_index = index
            self._batch_fraction = 0.
        item = self.batch_table.item(index, 3)
        if item is not None:
            item.setText(tr(state))
        count = self.batch_table.rowCount()
        if count:
            done = index + (state in ('已生成', '已跳过', '失败'))
            self.progress.setValue(round(done * 100 / count))
        if state == '处理中':
            self.batch_table.scrollToItem(self.batch_table.item(index, 0))

    def cancel(self):
        self.pause()
        if self.active:
            self._cancel.set()
            self.run_button.setEnabled(False)
            self._status('正在取消材质任务…')

    def _on_progress(self, stage, done, total):
        if self._task_kind == 'batch':
            count = self.batch_table.rowCount()
            if not self._cancel.is_set():
                self._status('一键处理 {index}/{count} · {stage} · {done}/{total}',
                             index=self._batch_index + 1, count=count, stage=tr(stage), done=done, total=total)
            self._batch_fraction = max(self._batch_fraction, min(.9, done / total * .9) if total else 0.)
            self.progress.setValue(round((self._batch_index + self._batch_fraction) * 100 / max(1, count)))
            return
        if not self._cancel.is_set():
            if total:
                self._status('{stage} · {done}/{total}', stage=tr(stage), done=done, total=total)
            else:
                self._status('{stage} · {done}', stage=tr(stage), done=done)
        self.progress.setValue(round(done * 100 / total) if total else 0)

    def _on_finished(self, kind, result, error):
        self.active = False
        self.progress.hide()
        if error is not None:
            if isinstance(error, InterruptedError):
                self._append(self._status('序列帧材质任务已取消。'))
            else:
                self._append(self._status('材质任务失败：{error}', error=display_text(exception_text(error))))
        elif kind == 'scan':
            self._scan = result
            self.texture_box.blockSignals(True)
            self.texture_box.clear()
            for choice in result.choices:
                rule = choice.rule
                label = f'{rule.part} - {tr(rule.layer_label)} - {choice.source.name}'
                self.texture_box.addItem(label, userData=choice)
            self.texture_box.blockSignals(False)
            self._selection_changed()
            self._populate_batch()
            self._append(self._status('扫描完成：{count} 个可选项，{skipped} 项已跳过。',
                                      count=len(result.choices), skipped=result.skipped))
            for warning in result.warnings:
                self._append(warning)
            if not result.choices:
                self._status('未找到配方支持的单帧 VTF，请检查输入目录和处理日志。')
        elif kind == 'batch':
            for choice, message in result.failed:
                self._append(str(choice.source) + '\n' + display_text(message))
            paths = result.created or result.skipped
            self._last_output = paths[-1] if paths else None
            self._append(self._status('一键处理结束：生成 {created}，跳过 {skipped}，失败 {failed}。',
                                      created=len(result.created), skipped=len(result.skipped), failed=len(result.failed)))
            if result.cancelled:
                self._append(tr('一键处理已取消，已完成的产物保留。'))
        else:
            product = result if kind == 'preview' else result[0]
            self._cached = product
            if kind == 'preview' or self._preview_loaded:
                self._preview_loaded = True
                self._frame = 0
                self.frame_slider.setRange(0, len(product.frames) - 1)
                self._show_frame()
                rule = product.choice.rule
                self.spec_label.setText(tr('{count} 帧 · {format}',
                    count=rule.frames, format='DXT1' if rule.image_format == 13 else 'DXT5'))
            psnr = '∞' if math.isinf(product.final_psnr) else f'{product.final_psnr:.2f}'
            self._append(tr('末帧编码 PSNR：{value} dB', value=psnr))
            if product.final_psnr < 40:
                self._append(tr('末帧编码 PSNR 低于 40 dB，请检查预览画质；此值不衡量与静态源图的相似度。'))
            if kind == 'preview':
                self._append(self._status('预览已加载，尚未写入输出目录。'))
            else:
                self._last_output = result[1]
                self._append(self._status('已生成一个 VTF：{path}', path=str(result[1])))
        self._refresh_buttons()
        self.busy_changed.emit(False)

    def _show_frame(self):
        if not self._preview_loaded or self._cached is None:
            return
        self.viewport.set_frame(self._cached.frames[self._frame], self._channel)
        self.frame_slider.blockSignals(True)
        self.frame_slider.setValue(self._frame)
        self.frame_slider.blockSignals(False)
        self.frame_label.setText(tr('{frame} / {count} 帧', frame=self._frame + 1,
                                    count=len(self._cached.frames)))

    def _set_channel(self, channel):
        self._channel = channel
        self.rgb_button.setChecked(channel == 'rgb')
        self.alpha_button.setChecked(channel == 'alpha')
        self._show_frame()

    def _seek(self, frame):
        self.pause()
        self._frame = frame
        self._show_frame()

    def _toggle_play(self):
        if self._playing:
            self.pause()
        elif self._preview_loaded and self._cached is not None:
            self._playing = True
            self._play_started = time.perf_counter()
            self._play_frame = self._frame
            self.play_button.setText(tr('暂停'))
            self._timer.start()

    def _tick(self):
        if not self._playing or self._cached is None:
            return
        frame = (self._play_frame + int((time.perf_counter() - self._play_started) * 30)) % len(self._cached.frames)
        if frame != self._frame:
            self._frame = frame
            self._show_frame()

    def pause(self):
        self._playing = False
        self._timer.stop()
        self.play_button.setText(tr('播放'))

    def hideEvent(self, event):
        self.pause()
        super().hideEvent(event)

    def _open_output(self):
        if self._last_output is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_output.parent)))
