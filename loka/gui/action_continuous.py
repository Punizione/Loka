"""Read-only playlist controls around the existing action preview viewport."""
import math

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, PushButton, DoubleSpinBox

from ..i18n import tr, display_text
from ..fps_values import fps_text
from .action_dialogs import ActionFormDialog


def _sequence_source_rate(page, index):
    source = page.session.source_index(page._model_index, index)
    try:
        action = page.session.action(page._model_index, index, all_slots=True)
        rate = page.session.effective_fps(page._model_index, index) / action.fps
    except ValueError:
        rate = 1.
    return source, rate


def _sequence_option(page, index, sequence):
    name = tr('{name}[顺位：{index}]', name=sequence.theName, index=index)
    try:
        source, rate = _sequence_source_rate(page, index)
        timeline = page._previews[page._model_index].timeline(source, rate)
        duration = timeline.duration if timeline.frame_count > 1 else 1.
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('Invalid sequence duration')
        return tr('{duration:.3f} 秒 · {name}', name=name, duration=duration)
    except (ValueError, IndexError):
        return tr('{name} · 时长不可用', name=name)


class ContinuousSelectionDialog(ActionFormDialog):
    def __init__(self, page):
        from ..action_playback import is_idle_sequence, is_shove_sequence
        super().__init__('分层动作预览', page, confirm_text='开始预览', width=900)
        note = BodyLabel(tr('主序列仅选 idle；叠加层 0 自由选择，叠加层 1 仅选推击。每条轨道最多 3 个动作，可重复选择。'), self.widget)
        note.setWordWrap(True)
        self.viewLayout.addWidget(note)
        self.track_boxes = []
        self.delay_boxes = []
        selected = page.continuous.tracks if page.continuous.active else ((), (), ())
        profile = page.session.models[page._model_index].profile_id
        sequences = page._previews[page._model_index].data.theSequenceDescs or []
        options = [_sequence_option(page, i, sequence) for i, sequence in enumerate(sequences)]
        if not page.continuous.active:
            current = page.sequence_box.currentData()
            idles = [i for i, s in enumerate(sequences) if is_idle_sequence(s, profile)]
            idle = current if current in idles else next(iter(idles), None)
            overlay = current if current is not None and current != idle else None
            shove = overlay is not None and is_shove_sequence(sequences[overlay], profile)
            selected = ((idle,) if idle is not None else (),
                        (overlay,) if overlay is not None and not shove else (),
                        (overlay,) if shove else ())
        labels = (tr('m_nSequence（仅 idle）'), 'm_nLayerSequence[0]', tr('m_nLayerSequence[1]（仅推击）'))
        delays = page.continuous.delays if page.continuous.active else (0., 0.)
        for lane in range(3):
            heading = QHBoxLayout()
            heading.addWidget(BodyLabel(labels[lane], self.widget))
            heading.addStretch()
            if lane:
                heading.addWidget(CaptionLabel(tr('开始延时'), self.widget))
                delay = DoubleSpinBox(self.widget)
                delay.setRange(0., 3600.)
                delay.setDecimals(3)
                delay.setSingleStep(.1)
                delay.setSuffix(tr(' 秒'))
                delay.setValue(delays[lane-1])
                heading.addWidget(delay)
                self.delay_boxes.append(delay)
            self.viewLayout.addLayout(heading)
            row = QHBoxLayout()
            boxes = []
            for slot in range(3):
                box = ComboBox(self.widget)
                box.setMinimumWidth(0)
                box.addItem(tr('不播放'), userData=None)
                for index, sequence in enumerate(sequences):
                    if lane == 0 and not is_idle_sequence(sequence, profile):continue
                    if lane == 2 and not is_shove_sequence(sequence, profile):continue
                    box.addItem(options[index], userData=index)
                chosen = selected[lane][slot] if slot < len(selected[lane]) else None
                box.setCurrentIndex(next((i for i in range(box.count()) if box.itemData(i) == chosen), 0))
                box.currentIndexChanged.connect(lambda _index, combo=box: combo.setToolTip(combo.currentText()))
                box.setToolTip(box.currentText())
                row.addWidget(box, 1)
                boxes.append(box)
            self.viewLayout.addLayout(row)
            self.track_boxes.append(boxes)
        self.error_label = CaptionLabel('', self.widget)
        self.viewLayout.addWidget(self.error_label)
        duration_note = CaptionLabel(tr('选项时长按当前有效 FPS 计算，表示单轮播放时间，不包含开始延时和末尾淡出。'), self.widget)
        duration_note.setWordWrap(True)
        self.viewLayout.addWidget(duration_note)
        hint = CaptionLabel(tr('连续预览期间不能编辑 FPS 或调换序列；原版模型保持静止，音效暂停。'), self.widget)
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)
        blending = CaptionLabel(tr('按主序列 → 层 0 → 层 1 叠加。每段播放一轮；叠加层结束后淡出，最后一个 idle 持续播放。单帧动作停留 1 秒。'), self.widget)
        blending.setWordWrap(True)
        self.viewLayout.addWidget(blending)
        self.validate_callback = self._validate_selection

    @property
    def selected_tracks(self):
        return tuple(tuple(box.currentData() for box in boxes if box.currentData() is not None) for boxes in self.track_boxes)

    @property
    def layer_delays(self):
        return tuple(box.value() for box in self.delay_boxes)

    def _validate_selection(self):
        valid = bool(self.selected_tracks[0])
        self.error_label.setText('' if valid else tr('主序列至少需要一个 idle 动作。'))
        return valid


class ContinuousPreview:
    def __init__(self, page, sequence_layout, layout):
        self.page = page
        self.playback = None
        self.tracks = ((), (), ())
        self._names = self._sources = self._rates = ((), (), ())
        self.delays = (0., 0.)
        self._slider_scale = 1000.
        self.button = PushButton(tr('分层动作预览…'), page.sequence_controls)
        sequence_layout.addWidget(self.button, 0, 4)
        self.controls = QWidget(page)
        row = QHBoxLayout(self.controls)
        row.setContentsMargins(0, 0, 0, 0)
        self.caption = CaptionLabel('', self.controls)
        self.caption.setWordWrap(True)
        row.addWidget(self.caption, 1)
        self.edit_button = PushButton(tr('重新选择'), self.controls)
        self.exit_button = PushButton(tr('退出连续预览'), self.controls)
        row.addWidget(self.edit_button)
        row.addWidget(self.exit_button)
        layout.addWidget(self.controls)
        self.controls.hide()
        self.button.clicked.connect(self.choose)
        self.edit_button.clicked.connect(self.choose)
        self.exit_button.clicked.connect(self.exit)

    @property
    def active(self):
        return self.playback is not None

    def choose(self):
        p = self.page
        if p._busy or p.suspended or p.viewport.model is None:
            return
        p.commit_fps_edit()
        p.pause()
        box = ContinuousSelectionDialog(p)
        try:
            if box.exec() == QDialog.Accepted:
                try:
                    self.start(*box.selected_tracks, layer_delays=box.layer_delays)
                except (ValueError, IndexError) as error:
                    p._viewport_warning(tr('连续预览无法启动：{error}', error=display_text(str(error))))
        finally:
            box.deleteLater()

    def start(self, main_indexes, layer0_indexes=(), layer1_indexes=(), *, layer_delays=(0., 0.)):
        from ..action_playback import LayeredPlayback, is_idle_sequence, is_shove_sequence
        p = self.page
        if p._busy or p.suspended or p.session is None or p.viewport.model is None:
            raise ValueError(tr('请先载入可预览的模型。'))
        tracks = tuple(tuple(indexes) for indexes in (main_indexes, layer0_indexes, layer1_indexes))
        if not 1 <= len(tracks[0]) <= 3 or any(len(track) > 3 for track in tracks):
            raise ValueError(tr('主序列需要 1 至 3 个 idle，每个叠加层最多 3 个动作。'))
        sequences = p._previews[p._model_index].data.theSequenceDescs or []
        if any(type(i) is not int or not 0 <= i < len(sequences) for indexes in tracks for i in indexes):
            raise ValueError(tr('所选动作顺位无效。'))
        profile = p.session.models[p._model_index].profile_id
        if any(not is_idle_sequence(sequences[i], profile) for i in tracks[0]):
            raise ValueError(tr('m_nSequence 只能选择 idle 类动作。'))
        if any(not is_shove_sequence(sequences[i], profile) for i in tracks[2]):
            raise ValueError(tr('m_nLayerSequence[1] 只能选择推击类动作。'))
        p.commit_fps_edit()
        sources = []
        scales = []
        for track in tracks:
            lane_sources, lane_scales = [], []
            for index in track:
                source, rate = _sequence_source_rate(p, index)
                lane_sources.append(source)
                lane_scales.append(rate)
            sources.append(tuple(lane_sources))
            scales.append(tuple(lane_scales))
        playback = LayeredPlayback(p.viewport.model, *sources, rate_scales=scales,
            layer_delays=layer_delays, profile_id=profile, semantic_indexes=tracks)
        p.pause()
        if not self.active:
            self._official_caption = p.official_label.text()
        self.tracks = tracks
        self._sources, self._rates = tuple(sources), tuple(scales)
        self._names = tuple(tuple(sequences[i].theName for i in track) for track in tracks)
        self.delays = tuple(float(v) for v in layer_delays)
        self.playback = playback
        self._slider_scale = min(1000., 2_000_000_000 / max(playback.duration, 1.))
        p._elapsed_seconds = 0.
        p._frame = 0.
        p.sequence_controls.hide()
        self.controls.show()
        p.comparison.hide()
        p.official_label.setText(tr('原版 V 模（连续预览期间保持静止）'))
        self.caption.setText('\n'.join(f'{label}: '+(' → '.join(names) if names else tr('不播放'))
            for label, names in zip(('m_nSequence', '[0]', '[1]'), self._names)))
        p._refresh_frame()

    def exit(self, _checked=False, *, refresh=True):
        if not self.active:
            return
        p = self.page
        p.pause()
        self.playback = None
        self.tracks = self._names = self._sources = self._rates = ((), (), ())
        self.delays = (0., 0.)
        self.controls.hide()
        p.sequence_controls.show()
        p.comparison.show()
        p.official_label.setText(self._official_caption)
        p._elapsed_seconds = p._frame = 0.
        if refresh:
            p._sequence_changed()
            p._update_controls()

    def state(self):
        return self.playback.state(self.page._elapsed_seconds, loop=self.page.loop_check.isChecked())

    def step_seconds(self):
        clip, _frame = self._focused_clip(self.state())
        return clip.duration / (clip.frame_count-1) if clip.frame_count > 1 else 1/30

    def _focused_clip(self, state):
        clip = self.playback.main.clips[state.main.clip_index]
        frame = state.main.frame
        for track, layer in zip(self.playback.layer_tracks, state.layers):
            if track is not None and layer.active and layer.clip_index is not None:
                clip = track.clips[layer.clip_index]
                frame = layer.frame
        return clip, frame

    def seek(self, value):
        self.page._elapsed_seconds = float(value) / self._slider_scale

    def step(self, offset):
        p = self.page
        p._elapsed_seconds = min(self.playback.duration, max(0., self.state().elapsed+offset*self.step_seconds()))
        p._refresh_frame()

    def sync_slider(self):
        p = self.page
        state = self.state()
        p.frame_slider.blockSignals(True)
        p.frame_slider.setRange(0, max(0, math.ceil(self.playback.duration*self._slider_scale)))
        p.frame_slider.setValue(round(state.elapsed*self._slider_scale))
        p.frame_slider.blockSignals(False)

    def refresh(self):
        p = self.page
        # Arm mounting can replace the render wrapper while preserving clips.
        if self.playback.model is not p.viewport.model:
            from ..action_playback import LayeredPlayback
            try:
                self.playback = LayeredPlayback(p.viewport.model, *self._sources, rate_scales=self._rates,
                    layer_delays=self.delays, profile_id=p.session.models[p._model_index].profile_id,
                    semantic_indexes=self.tracks)
            except (ValueError, IndexError, AttributeError) as error:
                self.exit(refresh=False)
                p._viewport_warning(tr('连续预览无法启动：{error}', error=display_text(str(error))))
                p._update_controls()
                return
        state = self.state()
        clip, frame = self._focused_clip(state)
        p._frame = frame
        self.sync_slider()
        names = [self._names[0][state.main.clip_index]]
        for lane, layer in enumerate(state.layers):
            if layer.active and layer.clip_index is not None:
                names.append(self._names[lane+1][layer.clip_index])
        caption = tr('分层预览 · {names}', names=' | '.join(names))
        p.current_label.setText(caption)
        p.current_label.setToolTip(caption)
        p.current_fps_label.setText(tr('FPS：{fps}', fps=fps_text(clip.fps)))
        p.info_label.setText(tr('总时间 {seconds:.2f} / {duration:.2f} 秒 · 主序列 {number}/{count}',
            number=state.main.clip_index+1, count=len(self.tracks[0]), seconds=state.elapsed, duration=self.playback.duration))
        p.frame_label.setText(tr('帧 {frame} / {last}', frame=int(frame), last=max(clip.frame_count-1, 0)))
        p.timing_label.setText(tr('主序列混合 {main:.0%} · 层 0 权重 {zero:.0%} · 层 1 权重 {one:.0%} · FPS 只读',
            main=state.main.transition_weight, zero=state.layers[0].weight, one=state.layers[1].weight))
        try:
            p.viewport.set_pose(self.playback.sample(p._elapsed_seconds, loop=p.loop_check.isChecked()))
            for message in p.viewport.model.warnings:
                p.viewport._report(message)
        except (ValueError, IndexError) as error:
            p.pause()
            p._viewport_warning(tr('连续预览求值失败：{error}', error=display_text(str(error))))
        p._update_controls()

    def update_controls(self, available):
        p = self.page
        loaded = (p.session is not None and p._loaded_models is p.session.models
                  and p.viewport.model is not None and bool(p.viewport.model.data.theSequenceDescs))
        enabled = not p._busy and loaded
        self.button.setEnabled(enabled)
        self.edit_button.setEnabled(enabled)
        self.exit_button.setEnabled(not p._busy)
        if self.active:
            for widget in (p.sequence_box, p.fps_input, p.swap_button, p.save_button,
                           p.compare_check, p.sound_check, p.model_switch_button):
                widget.setEnabled(False)
            for widget in (p.frame_slider, p.play_button, p.previous_button, p.next_button,
                           p.loop_check, p.camera_box, p.fov_slider):
                widget.setEnabled(enabled)
