"""Hidden action correction controls; one frame clock drives view and bars."""
import math
import time

from PySide6.QtCore import Qt, Signal, QTimer, QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFileDialog, QDialog, QSplitter, QSizePolicy
from qfluentwidgets import (BodyLabel, CaptionLabel, ComboBox,
    PrimaryPushButton, PushButton, Slider, CheckBox, IndeterminateProgressBar,
    SubtitleLabel, TextEdit)
from ..fps_values import fps_text,round_fps
from ..i18n import tr, display_text
from .action_dialogs import ActionFormDialog
from .action_fps import ActionFpsInput


def sequence_option_text(action):
    return tr('{name}[顺位：{index}]({kind})', name=display_text(action.display_name),
              index=action.index, kind=tr('官方动作') if action.official_index is not None else tr('额外动作'))+'  ·  '+action.activity


def choose_model_profile(parent,model_path):
    from ..action_standalone import infer_profile
    from ..weapon_sequences import _profiles
    from ..weapon_catalog import get_weapon
    inferred=infer_profile(model_path)
    box=ActionFormDialog(tr('确认 V 模武器类型'),parent);layout=box.viewLayout
    layout.addWidget(BodyLabel(tr('选择该 V 模对应的原版武器，用于动作规则与原版对比：'),box))
    choices=ComboBox(box)
    choices.addItem(tr('请选择武器类型'),userData=None)
    for key in _profiles():
        label=tr('小手枪（双持）') if key=='dual_pistol' else get_weapon(key).name
        choices.addItem(display_text(label),userData=key)
    if inferred:
        choices.setCurrentIndex(next(i for i in range(choices.count()) if choices.itemData(i)==inferred))
    layout.addWidget(choices)
    box.yesButton.setEnabled(choices.currentData() is not None)
    choices.currentIndexChanged.connect(lambda *_:box.yesButton.setEnabled(choices.currentData() is not None))
    return choices.currentData() if box.exec()==QDialog.Accepted else None


def confirmation_text(session):
    lines = []
    for change in session.changes:
        if change.removed:
            lines.append(tr('{profile}  剔除额外槽位 {name}[顺位：{index}]（当前内容来自原顺位 {source}）',
                profile=change.model.profile_id,name=change.action.name,index=change.action.index,
                source=session.source_index(change.model_index,change.action.index)))
            continue
        kind = tr('官方') if change.action.official_index is not None else tr('额外')
        source=session.source_index(change.model_index,change.action.index)
        if source!=change.action.index:
            lines.append(tr('{profile}  顺位 {index} ← 原顺位 {source} （{name}，保留槽位名称和 Activity）',
                         profile=change.model.profile_id,index=change.action.index,source=source,
                         name=display_text(change.action.display_name)))
        lines.append(tr('{profile}  [{name},顺位：{index},{kind}] {old} -> {new} FPS',
                     profile=change.model.profile_id,name=display_text(change.action.display_name),
                     index=change.action.index,kind=kind,old=fps_text(change.old_fps),new=fps_text(change.new_fps)))
    return '\n'.join(lines)


class DurationComparison(QWidget):
    """Bar lengths share seconds; each row retains its own frame ticks."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self.official_duration = None
        self.model_duration = 0.
        self.official_frames = None
        self.model_frames = 1
        self.seconds = 0.
        self.official_seconds=0.;self.model_seconds=0.
        self.official_note=tr('未载入原版动画')
        self.setMinimumHeight(78)

    def set_times(self,official,model,official_frames,model_frames,seconds,*,official_seconds=None,model_seconds=None,official_note='未载入原版动画'):
        self.official_duration, self.model_duration = official, max(float(model),0.)
        self.official_frames, self.model_frames = official_frames, model_frames
        self.seconds = max(float(seconds),0.)
        self.official_seconds=self.seconds if official_seconds is None else max(float(official_seconds),0.)
        self.model_seconds=self.seconds if model_seconds is None else max(float(model_seconds),0.)
        self.official_note=official_note
        self.update()

    @property
    def ratios(self):
        extent = max(self.official_duration or 0.,self.model_duration)
        return ((self.official_duration or 0.)/extent,self.model_duration/extent) if extent else (0.,0.)

    @property
    def cursor_ratio(self):
        extent = max(self.official_duration or 0.,self.model_duration)
        return min(self.seconds/extent,1.) if extent else 0.

    def paintEvent(self,event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        font = self.font();font.setPointSizeF(max(font.pointSizeF()-1,8));painter.setFont(font)
        label_width = 142
        width = max(self.width()-label_width-12,1)
        colors = (QColor('#6aa9d8'),QColor('#67c8b0'))
        for row,(duration,frames,ratio) in enumerate(zip(
                (self.official_duration,self.model_duration),(self.official_frames,self.model_frames),self.ratios)):
            y = 6+row*35
            painter.setPen(self.palette().text().color())
            label = tr('原版动画') if row==0 else tr('当前 V 模')
            painter.drawText(0,y+14,label)
            painter.setPen(Qt.NoPen);painter.setBrush(QColor(110,120,130,40))
            painter.drawRoundedRect(QRectF(label_width,y,width,16),4,4)
            painter.setBrush(colors[row]);length = width*ratio
            painter.drawRoundedRect(QRectF(label_width,y,length,16),4,4)
            painter.setPen(self.palette().text().color())
            seconds=self.official_seconds if row==0 else self.model_seconds
            progress=min(seconds,duration) if duration is not None else 0.
            text = tr('{duration:.2f} 秒',duration=duration) if duration is not None else display_text(self.official_note)
            if frames is not None:
                end=max(frames-1,0);frame=progress/duration*end if duration else 0.
                text += tr('   帧 {frame} / {last}',frame=min(int(frame+1e-6),end),last=end)
            painter.drawText(label_width,y+30,text)
            if frames and frames>1 and length>20:
                painter.setPen(QColor(0,0,0,65))
                for tick in range(1,min(frames,11)):
                    x=label_width+length*tick/min(frames-1,10)
                    painter.drawLine(int(x),y+5,int(x),y+15)
            if duration is not None:
                painter.setPen(QPen(QColor('#e4a74e'),2))
                extent=max(self.official_duration or 0.,self.model_duration)
                x=label_width+width*(progress/extent if extent else 0.)
                painter.drawLine(int(x),y-2,int(x),y+18)


class _LogPanel:
    def __init__(self,page):
        self.page=page
        self.messages=[]

    def append(self,message):
        if not str(message).strip():return
        self.messages.append(str(message))
        self.page._refresh_details()
        self.page.status_label.setText(display_text(str(message).splitlines()[-1]))


class ActionCorrectionInterface(QWidget):
    back_requested = Signal()
    save_requested = Signal()
    arms_loaded=Signal(object)
    arms_failed=Signal(str)
    original_loaded=Signal(object)
    original_failed=Signal(object)
    def __init__(self,parent=None,*,config=None):
        super().__init__(parent)
        self.setObjectName('actionCorrectionInterface')
        self.session=None
        self._model_index=0
        self._previews=[];self._materials=[]
        self._loaded_models=None
        self._busy=False;self._loading_values=False
        self._last_tick=0.;self._frame=0.;self._elapsed_seconds=0.;self._detail_lines=[]
        self._warnings=[]
        self._reference_details={}
        self._arms_asset=None;self._arms_worker=None
        self._original_asset=None;self._original_key=None;self._original_pending=None;self._original_error=None
        self._original_assets={}
        self._config=config
        from .action_audio import ActionAudio
        self.audio=ActionAudio(self)
        layout=QVBoxLayout(self);layout.setContentsMargins(20,14,20,12);layout.setSpacing(7)
        header=QHBoxLayout()
        header.addWidget(SubtitleLabel(tr('动作矫正'),self));header.addSpacing(12)
        self.open_model_button=PushButton(tr('选择 V 模…'),self)
        self.compare_check=CheckBox(tr('原版模型对比'),self)
        self.weapon_name_label=BodyLabel(tr('未载入武器'),self)
        self.weapon_name_label.setWordWrap(False);self.weapon_name_label.setMinimumWidth(180)
        self.weapon_name_label.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Preferred)
        header.addWidget(self.open_model_button);header.addWidget(self.weapon_name_label,1)
        self.back_button=PushButton(tr('清空预览并返回武器转模'),self)
        header.addWidget(self.back_button)
        self.save_button=PrimaryPushButton(tr('保存'),self)
        self.run_button=self.save_button
        header.addWidget(self.save_button);layout.addLayout(header)
        grid=QGridLayout();grid.setColumnStretch(1,1);grid.setColumnStretch(3,1)
        self.model_switch_button=PushButton(tr('切换单/双持'),self);self.model_switch_button.hide()
        self.camera_box=ComboBox(self)
        self.camera_box.addItem(tr('固定第一人称视角'),userData='fixed')
        self.camera_box.addItem(tr('跟随模型摄像头骨骼'),userData='follow')
        self.fov_slider=Slider(Qt.Horizontal,self);self.fov_slider.setRange(70,160);self.fov_slider.setValue(90)
        self.fov_caption=CaptionLabel(tr('预览距离'),self);self.fov_value=CaptionLabel('90',self)
        self.sequence_box=ComboBox(self)
        self.fps_input=ActionFpsInput(self)
        self.fps_label=CaptionLabel('FPS',self)
        grid.addWidget(CaptionLabel(tr('摄像头'),self),0,0);grid.addWidget(self.camera_box,0,1)
        lens=QHBoxLayout();lens.addWidget(self.fov_slider,1);lens.addWidget(self.fov_value)
        grid.addWidget(self.fov_caption,0,2);grid.addLayout(lens,0,3)
        tools=QHBoxLayout()
        self.arms_button=PushButton(tr('挂载手臂模型'),self)
        self.remove_arms_button=PushButton(tr('移除手臂'),self)
        self.arms_label=CaptionLabel(tr('未挂载额外手臂'),self)
        self.swap_button=PushButton(tr('序列调换'),self)
        self.removal_button=PushButton(tr('序列剔除'),self)
        for widget in (self.model_switch_button,self.arms_button,self.remove_arms_button,self.arms_label):tools.addWidget(widget)
        tools.addStretch();tools.addWidget(self.compare_check);grid.addLayout(tools,1,0,1,4)
        layout.addLayout(grid)
        from .action_viewport import ActionViewport
        self.preview_splitter=QSplitter(Qt.Horizontal,self)
        self.official_panel=QWidget(self.preview_splitter);official_layout=QVBoxLayout(self.official_panel)
        official_layout.setContentsMargins(0,0,0,0)
        self.official_label=CaptionLabel(tr('原版 V 模'),self.official_panel)
        self.official_label.setMinimumHeight(self.compare_check.sizeHint().height())
        self.official_viewport=ActionViewport(self.official_panel)
        official_layout.addWidget(self.official_label);official_layout.addWidget(self.official_viewport,1)
        self.current_panel=QWidget(self.preview_splitter);current_layout=QVBoxLayout(self.current_panel)
        current_layout.setContentsMargins(0,0,0,0)
        self.current_label=CaptionLabel(tr('当前 V 模 · 未选择动作'),self.current_panel)
        self.current_label.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        self.current_label.setMinimumHeight(self.official_label.minimumHeight())
        self.current_fps_label=CaptionLabel(tr('FPS：—'),self.current_panel)
        current_header=QHBoxLayout()
        current_header.addWidget(self.current_label,1);current_header.addWidget(self.current_fps_label)
        self.viewport=ActionViewport(self.current_panel)
        self.viewport.setMinimumHeight(170)
        current_layout.addLayout(current_header);current_layout.addWidget(self.viewport,1)
        self.preview_splitter.addWidget(self.official_panel);self.preview_splitter.addWidget(self.current_panel)
        self.preview_splitter.setStretchFactor(0,1);self.preview_splitter.setStretchFactor(1,1)
        self.official_panel.hide();layout.addWidget(self.preview_splitter,1)
        self.info_label=BodyLabel(tr('正在载入成品 V 模…'),self)
        self.info_label.setWordWrap(True);layout.addWidget(self.info_label)
        self.preview_note_label=CaptionLabel(tr('预览动作速度不一定完全匹配游戏内动作速度，仅供参考。'),self)
        self.preview_note_label.setWordWrap(True);layout.addWidget(self.preview_note_label)
        self.timing_label=CaptionLabel('',self);self.timing_label.setWordWrap(True)
        layout.addWidget(self.timing_label)
        self.comparison=DurationComparison(self);layout.addWidget(self.comparison)
        self.frame_slider=Slider(Qt.Horizontal,self);self.frame_slider.setRange(0,0)
        layout.addWidget(self.frame_slider)
        playback=QHBoxLayout()
        self.play_button=PushButton(tr('播放'),self)
        self.previous_button=PushButton(tr('上一帧'),self);self.next_button=PushButton(tr('下一帧'),self)
        self.loop_check=CheckBox(tr('循环播放'),self)
        self.sound_check=CheckBox(tr('播放音效'),self)
        self.sound_check.setToolTip(tr('仅播放当前 V 模音效，只读取零散文件：模型资源 → 已解包 L4N → 原版资源。'))
        self.frame_label=CaptionLabel(tr('帧 0 / 0'),self)
        for widget in (self.play_button,self.previous_button,self.next_button,self.loop_check,self.sound_check,self.frame_label):
            playback.addWidget(widget)
        playback.addStretch()
        self.cancel_button=PushButton(tr('取消任务'),self)
        self.cancel_button.setVisible(False)
        playback.addWidget(self.swap_button);playback.addWidget(self.removal_button);playback.addWidget(self.cancel_button)
        layout.addLayout(playback)
        self.sequence_controls=QWidget(self)
        sequence_layout=QGridLayout(self.sequence_controls);sequence_layout.setContentsMargins(0,0,0,0)
        sequence_layout.setColumnStretch(1,1)
        sequence_layout.addWidget(CaptionLabel(tr('动作序列'),self.sequence_controls),0,0)
        sequence_layout.addWidget(self.sequence_box,0,1)
        sequence_layout.addWidget(self.fps_label,0,2);sequence_layout.addWidget(self.fps_input,0,3)
        self.fps_input.setMaximumWidth(150)
        layout.addWidget(self.sequence_controls)
        from .action_continuous import ContinuousPreview
        self.continuous=ContinuousPreview(self,sequence_layout,layout)
        self.progress=IndeterminateProgressBar(self);self.progress.hide();layout.addWidget(self.progress)
        status=QHBoxLayout()
        self.status_label=CaptionLabel('',self);self.status_label.setWordWrap(True)
        self.details_button=PushButton(tr('模型与引用详情'),self)
        self.swap_list_button=PushButton(tr('查看调换列表'),self)
        status.addWidget(self.status_label,1);status.addWidget(self.swap_list_button)
        status.addWidget(self.details_button);layout.addLayout(status)
        self.details_text=TextEdit(self);self.details_text.setReadOnly(True)
        self.details_text.setMaximumHeight(110);self.details_text.hide();layout.addWidget(self.details_text)
        self.log_card=_LogPanel(self)
        self._timer=QTimer(self);self._timer.setInterval(16);self._timer.timeout.connect(self._tick)
        self.model_switch_button.clicked.connect(lambda:self.select_model((self._model_index+1)%len(self.session.models)) if self.session else None)
        self.sequence_box.currentIndexChanged.connect(self._sequence_changed)
        self.camera_box.currentIndexChanged.connect(self._camera_changed)
        self.fov_slider.valueChanged.connect(self._fov_changed)
        self.fps_input.valueChanged.connect(self._fps_changed)
        self.fps_input.editingFinished.connect(self._update_controls)
        self.fps_input.lineEdit().textEdited.connect(lambda *_:self._update_controls())
        self.frame_slider.sliderPressed.connect(self.pause)
        self.frame_slider.valueChanged.connect(self._seek)
        self.play_button.clicked.connect(self._play_pause)
        self.previous_button.clicked.connect(lambda:self._step(-1))
        self.next_button.clicked.connect(lambda:self._step(1))
        self.back_button.clicked.connect(self._back)
        self.save_button.clicked.connect(self._confirm_save)
        self.cancel_button.clicked.connect(lambda:self.window()._cancel_event.set())
        self.details_button.clicked.connect(self._toggle_details)
        self.viewport.warning.connect(self._viewport_warning)
        self.official_viewport.warning.connect(lambda message:self._viewport_warning(tr('原版预览：{message}',message=display_text(message))))
        self.arms_button.clicked.connect(self._choose_arms)
        self.remove_arms_button.clicked.connect(self._remove_arms)
        self.swap_button.clicked.connect(self._choose_swap)
        self.removal_button.clicked.connect(self._choose_removal)
        self.swap_list_button.clicked.connect(self._show_swap_comparison)
        self.arms_loaded.connect(self._accept_arms)
        self.arms_failed.connect(self._fail_arms)
        self.compare_check.toggled.connect(self._comparison_toggled)
        self.original_loaded.connect(self._accept_original);self.original_failed.connect(self._fail_original)
        self.sound_check.toggled.connect(self._sound_toggled)
        self.audio.state_changed.connect(self._audio_state_changed)
        self.audio.failed.connect(self._audio_failed)
        self.open_model_button.clicked.connect(lambda:self.window().choose_action_model())
        if config is not None:self._bind_preferences(config)
        self.set_busy(False)

    def _bind_preferences(self,config):
        # Restore before adding persistence handlers: opening a page is read-only.
        checks=((self.compare_check,config.action_compare_original),
                (self.loop_check,config.action_loop),(self.sound_check,config.action_play_audio))
        for box,item in checks:box.setChecked(config.get(item))
        self.camera_box.setCurrentIndex(next(i for i in range(self.camera_box.count())
            if self.camera_box.itemData(i)==config.get(config.action_camera_mode)))
        self.fov_slider.setValue(config.get(config.action_preview_distance))
        self._set_details_visible(config.get(config.action_details_open))
        for box,item in checks:
            box.toggled.connect(lambda checked,setting=item:config.set(setting,checked))
        self.camera_box.currentIndexChanged.connect(lambda i:
            config.set(config.action_camera_mode,self.camera_box.itemData(i)))
        self.fov_slider.valueChanged.connect(lambda value:config.set(config.action_preview_distance,value))

    def current_action(self):
        if not self.session:return None
        index=self.sequence_box.currentData()
        if index is None:return None
        return self.session.action(self._model_index,index)

    def _sound_toggled(self,checked):
        self.pause();self.audio.set_enabled(checked)

    def _audio_state_changed(self):
        self.sound_check.setText(tr('播放音效（载入中…）') if self.audio.loading and self.audio.enabled else tr('播放音效'))
        self._refresh_details();self._update_controls()

    def _audio_failed(self,message):
        self.sound_check.blockSignals(True);self.sound_check.setChecked(False);self.sound_check.blockSignals(False)
        if self._config is not None:self._config.set(self._config.action_play_audio,False)
        self._viewport_warning(message)
        self._update_controls()

    def _advance_audio(self,previous,*,include_start=False):
        action=self.current_action()
        if action is None:return
        self.audio.advance(self._model_index,self.session.source_index(self._model_index,action.index),
            previous,self._elapsed_seconds,self.current_timeline().duration,
            loop=self.loop_check.isChecked(),include_start=include_start)

    def select_model(self,index):
        if self._busy or self.session is None or self._loaded_models is not self.session.models:return
        if type(index) is not int or not 0<=index<len(self.session.models):raise ValueError(tr('无效的武器模型。'))
        self.commit_fps_edit()
        if index!=self._model_index:self._model_index=index;self._model_changed()

    def commit_fps_edit(self):
        if self.continuous.active:return
        if self._busy or self._loading_values or not self.current_action() or self._loaded_models is not self.session.models:return
        self.fps_input.interpretText();self._update_controls()

    def _fov_changed(self,value):
        self.fov_value.setText(str(value))
        self.viewport.set_fov(value);self.official_viewport.set_fov(value)

    def _comparison_toggled(self,checked):
        if self.continuous.active:return
        self.pause();self.official_panel.setVisible(checked)
        if checked:
            self._original_error=None
            width=max(self.preview_splitter.width()//2,1);self.preview_splitter.setSizes([width,width])
            self._ensure_original()
            if self._original_key==self._comparison_key():self._apply_original_model()
        self._refresh_frame()

    def _comparison_key(self):
        if not self.session or self._model_index<0:return None
        index=self._model_index;model=self.session.models[index]
        return id(self.session),index,model.profile_id,self.session.options.game_path

    def _ensure_original(self):
        if self.continuous.active:return
        key=self._comparison_key()
        if (not self.compare_check.isChecked() or key is None or self.suspended or self._busy or self._original_pending
                or self._original_key==key or self._original_error and self._original_error[0]==key):return
        window=self.window()
        if not hasattr(window,'_start_worker') or window._worker_busy():return
        self.pause();self._original_pending=key;self.official_label.setText(tr('正在载入原版 V 模…'))
        self._original_asset=None;self.official_viewport.set_model(None,())
        def work():
            try:
                from ..action_compare import load_official_viewmodel
                asset=load_official_viewmodel(key[2],key[3],cancelled=window._cancel_event.is_set)
                if window._cancel_event.is_set():raise InterruptedError(tr('原版模型载入已取消。'))
                self.original_loaded.emit((key,asset));window._signals.done.emit(tr('原版对比模型已载入。'))
            except Exception as error:
                self.original_failed.emit((key,str(error)));window._signals.done.emit(tr('原版模型载入失败：{error}',error=display_text(str(error))))
        window._start_worker(self,work,tr('载入原版对比模型'))

    def _accept_original(self,payload):
        key,asset=payload;self._original_pending=None
        if key!=self._comparison_key():return
        self._original_key=key;self._original_asset=asset;self._original_error=None
        self._original_assets[key[1]]=asset
        if self.compare_check.isChecked():self._apply_original_model()
        self._refresh_frame()

    def _fail_original(self,payload):
        key,message=payload;self._original_pending=None
        if key!=self._comparison_key():return
        self._original_error=(key,message);self.official_label.setText(tr('原版模型载入失败：{error}',error=display_text(message)))
        self._refresh_frame()

    def _apply_original_model(self):
        if self._original_asset is None:return
        asset=self._original_asset;model=asset.model;materials=tuple(asset.materials)
        if self._arms_asset is not None:
            from ..action_arms import CompositePreviewModel
            model=CompositePreviewModel(model,self._arms_asset);materials+=self._arms_asset.materials
        reference=self.official_viewport._camera_reference
        self.official_viewport.set_model(model,materials)
        if reference is not None:self.official_viewport._camera_reference=reference
        self.official_viewport.set_camera_mode(self.camera_box.currentData())

    def _official_sequence(self):
        action=self.current_action()
        if not action or self._original_key!=self._comparison_key() or self._original_asset is None:return None
        from ..action_compare import matching_sequence
        return matching_sequence(self._original_asset.model,action.activity,action.official_index)

    def _official_clock(self):
        index=self._official_sequence()
        return self._original_asset.model.timeline(index) if index is not None else None

    def _refresh_original(self):
        if self.continuous.active:return
        if not self.compare_check.isChecked() or self._original_key!=self._comparison_key() or self._original_asset is None:return
        from ..action_compare import sample_sequence_frame
        asset=self._original_asset;index=self._official_sequence()
        if index is None:
            sequences=asset.model.data.theSequenceDescs or []
            if sequences:
                self.official_viewport.set_frame(0,0,frame_span=max(asset.model.timeline(0).frame_count-1,0))
            self.official_label.setText(tr('原版 V 模：无匹配动作，保持静止'))
            return
        clock=asset.model.timeline(index)
        frame=sample_sequence_frame(asset.model,index,self.playhead_seconds,loop=self.loop_check.isChecked())
        self.official_viewport.set_frame(index,frame,frame_span=max(clock.frame_count-1,0))
        self.official_label.setText(tr('原版 V 模 · {label} · {sequence}',label=display_text(asset.label),
                                      sequence=asset.model.data.theSequenceDescs[index].theName))

    def _apply_render_model(self,*,preserve_camera=False):
        index=self._model_index
        model=self._previews[index];materials=tuple(self._materials[index])
        if self._arms_asset is not None:
            from ..action_arms import CompositePreviewModel
            model=CompositePreviewModel(model,self._arms_asset)
            materials+=self._arms_asset.materials
        reference=self.viewport._camera_reference if preserve_camera else None
        self.viewport.set_model(model,materials)
        if reference is not None:self.viewport._camera_reference=reference
        self.arms_label.setText(self._arms_asset.label if self._arms_asset else tr('未挂载额外手臂'))

    def _choose_arms(self):
        from ..action_arms import OFFICIAL_ARMS
        box=ActionFormDialog(tr('挂载手臂模型'),self);layout=box.viewLayout
        layout.addWidget(BodyLabel(tr('选择游戏原版手臂，或从任意目录选择 MDL：'),box))
        choices=ComboBox(box)
        for key,preset in OFFICIAL_ARMS.items():choices.addItem(preset.label,userData=key)
        choices.addItem(tr('从文件选择…'),userData='custom')
        layout.addWidget(choices)
        if box.exec()!=QDialog.Accepted:return
        source=choices.currentData()
        if source=='custom':
            source,_=QFileDialog.getOpenFileName(self,tr('选择手臂模型'),'',tr('Source 模型 (*.mdl)'))
            if not source:return
        self.load_arms(source)

    def load_arms(self,source):
        window=self.window()
        if self._busy or self.suspended or self._arms_worker is not None or not self.session:return
        if window._worker_busy():return
        self.pause();self._arms_worker=True
        session=self.session
        def work():
            try:
                from ..action_arms import load_arms
                asset=load_arms(source,game_path=session.options.game_path,cancelled=window._cancel_event.is_set)
                if window._cancel_event.is_set():raise InterruptedError(tr('手臂载入已取消。'))
                self.arms_loaded.emit((session,asset))
                window._signals.done.emit(tr('预览手臂已挂载。'))
            except Exception as error:
                self.arms_failed.emit(str(error))
                window._signals.done.emit(tr('手臂载入失败：{error}',error=display_text(str(error))))
        window._start_worker(self,work,tr('载入预览手臂'))

    def _accept_arms(self,payload):
        self._arms_worker=None
        session,asset=payload
        if self.session is not session:return
        self._arms_asset=asset
        self._apply_render_model(preserve_camera=True);self._apply_original_model();self._refresh_frame();self._update_controls()

    def _fail_arms(self,message):
        self._arms_worker=None
        self.log_card.append(tr('手臂载入失败：{error}',error=display_text(message)));self._update_controls()

    def _remove_arms(self):
        if self._busy:return
        self._arms_asset=None
        if self.session:self._apply_render_model(preserve_camera=True);self._apply_original_model();self._refresh_frame()
        self._update_controls()

    def swap_sequences(self,first,second,*,timing_slots=None):
        if self.continuous.active:return
        if self._busy or not self.session or self.suspended:return
        self.pause()
        model_index=self._model_index
        self.session.swap_sequences(model_index,first,second,preview=self._previews[model_index],timing_slots=timing_slots)
        self._sequence_changed()

    def _show_swap_comparison(self):
        if (self._busy or self.session is None
                or not 0<=self._model_index<len(self.session.models)):return
        self.commit_fps_edit();self.pause()
        from .action_dialogs import SequenceSwapComparisonDialog
        box=SequenceSwapComparisonDialog(self.session,self._model_index,self)
        try:box.exec()
        finally:box.deleteLater()

    def _choose_swap(self):
        if self._busy or not self.session:return
        from ..action_swaps import validate_swap_pair
        model_index=self._model_index;model=self.session.models[model_index]
        actions=tuple(a for a in model.swap_actions or model.actions
                      if (model_index,a.index) not in self.session.removed_sequences)
        if len(actions)<2:return
        box=ActionFormDialog(tr('序列调换'),self,width=740);layout=box.viewLayout
        description=BodyLabel(tr('相同 Activity 交换整套动作；近战 SHOOT_LAYER 与三方向 HIT 可互换。目标有官方计时则匹配时长，无计时则保留换入动作 FPS。'),box)
        description.setWordWrap(True);layout.addWidget(description)
        first=ComboBox(box);second=ComboBox(box)
        for action in actions:first.addItem(sequence_option_text(action)+'  '+action.name,userData=action.index)
        current=self.sequence_box.currentData()
        first.setCurrentIndex(next((i for i,a in enumerate(actions) if a.index==current),0))
        layout.addWidget(CaptionLabel(tr('目标槽位一'),box));layout.addWidget(first)
        layout.addWidget(CaptionLabel(tr('目标槽位二'),box));layout.addWidget(second)
        timing_boxes={}
        for slot in (0,1):
            label=CaptionLabel(tr('目标槽位一的计时参考（可选）') if slot==0 else tr('目标槽位二的计时参考（可选）'),box)
            combo=ComboBox(box);layout.addWidget(label);layout.addWidget(combo)
            timing_boxes[slot]=(label,combo)
        message=CaptionLabel('',box);message.setWordWrap(True);layout.addWidget(message)
        def refresh_times(*_):
            a=self.session.action(model_index,first.currentData(),all_slots=True)
            b_index=second.currentData();cross=False
            if b_index is not None:cross=a.activity!=self.session.action(model_index,b_index,all_slots=True).activity
            valid=b_index is not None
            for slot,index in enumerate((first.currentData(),b_index)):
                label,combo=timing_boxes[slot];label.setVisible(cross);combo.setVisible(cross)
                combo.clear()
                if not cross or index is None:continue
                action=self.session.action(model_index,index,all_slots=True)
                choices=self.session.timing_choices(model_index,index)
                combo.setEnabled(action.official_index is None)
                if action.official_index is not None:
                    combo.addItem(tr('{duration:.2f} 秒（官方槽位固定计时）',duration=action.official_duration) if action.official_duration else tr('无已确认计时（保留换入动作 FPS）'),
                                  userData=action.official_index)
                    continue
                combo.addItem(tr('不指定计时参考（无计时则保留换入动作 FPS）'),userData=None)
                for official,name,duration in choices:combo.addItem(tr('{name}[顺位：{index}]  {duration:.2f} 秒',
                    name=display_text(name),index=official,duration=duration),userData=official)
                current_timer=self.session.timing_index(model_index,index)
                chosen=next((i+1 for i,(official,_,duration) in enumerate(choices)
                             if official==current_timer),None)
                if chosen is None and action.official_duration is not None:
                    chosen=next((i+1 for i,(_,_,duration) in enumerate(choices)
                                 if math.isclose(duration,action.official_duration)),None)
                if chosen is None and len({duration for _,_,duration in choices})==1 and choices:chosen=1
                if chosen is not None:combo.setCurrentIndex(chosen)
            message.setText(tr('事件、IK、淡入淡出等内容一起调换；有官方计时的目标重算 FPS，无计时的目标保留换入动作 FPS。') if valid else tr('没有符合调换规则的另一个槽位。'))
            box.yesButton.setEnabled(valid)
        def fill_second(*_):
            second.blockSignals(True);second.clear()
            a=self.session.action(model_index,first.currentData(),all_slots=True)
            for candidate in actions:
                if candidate.index==a.index:continue
                try:validate_swap_pair(a,self.session.action(model_index,candidate.index,all_slots=True),model.profile_id)
                except ValueError:continue
                second.addItem(sequence_option_text(candidate)+'  '+candidate.name,userData=candidate.index)
            second.blockSignals(False);refresh_times()
        def apply():
            slots={}
            for slot,index in enumerate((first.currentData(),second.currentData())):
                label,combo=timing_boxes[slot]
                if not label.isHidden():
                    if self.session.baseline_action(model_index,index,all_slots=True).official_index is not None:continue
                    if combo.currentData() is not None:slots[index]=combo.currentData()
            try:self.swap_sequences(first.currentData(),second.currentData(),timing_slots=slots)
            except (ValueError,OSError) as error:message.setText(display_text(str(error)));return False
            return True
        first.currentIndexChanged.connect(fill_second);second.currentIndexChanged.connect(refresh_times)
        box.validate_callback=apply;fill_second();box.exec()

    def set_removed_sequences(self,indices):
        if self._busy or not self.session or self.continuous.active:return
        self.commit_fps_edit();self.pause()
        self.session.set_removals(self._model_index,indices)
        self._model_changed()

    def _choose_removal(self):
        if self._busy or not self.session or self.continuous.active:return
        from .action_removal import SequenceRemovalDialog
        self.commit_fps_edit();self.pause()
        box=SequenceRemovalDialog(self)
        try:box.exec()
        finally:box.deleteLater()

    @property
    def effective_fps(self):
        action=self.current_action()
        if action is None:return 30.
        return self.session.effective_fps(self._model_index,action.index)

    @property
    def playhead_seconds(self):
        return self._elapsed_seconds

    def _seconds_from_frame(self):
        timeline=self.current_timeline()
        span=max(timeline.frame_count-1,0) if timeline else 0
        return self._frame*timeline.duration/span if span else 0.

    def _playback_end(self):
        if self.continuous.active:return self.continuous.playback.duration
        timeline=self.current_timeline();end=timeline.duration if timeline else 0.
        official=self._official_clock()
        if official is not None:end=max(end,official.duration)
        return end

    def _frame_step_seconds(self):
        if self.continuous.active:return self.continuous.step_seconds()
        timeline=self.current_timeline()
        if timeline and timeline.duration>0 and timeline.frame_count>1:
            return timeline.duration/(timeline.frame_count-1)
        index=self._official_sequence()
        if index is not None:
            clock=self._original_asset.model.timeline(index)
            if clock.duration>0 and clock.frame_count>1:return clock.duration/(clock.frame_count-1)
        return 1/30

    def _sync_playback_slider(self):
        if self.continuous.active:
            self.continuous.sync_slider();return
        step=self._frame_step_seconds();extent=self._playback_end()
        maximum=max(0,math.ceil(extent/step-1e-8))
        position=self._elapsed_seconds
        if self.loop_check.isChecked() and extent>0:position%=extent
        self.frame_slider.blockSignals(True)
        self.frame_slider.setRange(0,maximum)
        self.frame_slider.setValue(min(maximum,round(position/step)))
        self.frame_slider.blockSignals(False)

    def current_timeline(self):
        action=self.current_action()
        if action is None or self.suspended:return None
        model_index=self._model_index
        return self._previews[model_index].timeline(
            self.session.source_index(model_index,action.index),rate_scale=self.effective_fps/action.fps)

    def set_loaded(self,payload):
        self.continuous.exit(refresh=False)
        previous=(self._model_index,self.sequence_box.currentData(),self._frame,self._elapsed_seconds)
        same=self.session is payload[0]
        self.pause();self.session,self._previews,self._materials=payload[:3]
        cameras=getattr(self,"_suspended_cameras",None) if same else None
        self._suspended_cameras=None
        if len(payload)>3:self._original_assets=payload[3]
        elif not same:self._original_assets={}
        if not same:self._arms_asset=None
        self._reference_details.clear()
        self._loaded_models=self.session.models
        self.viewport.setUpdatesEnabled(True);self.official_viewport.setUpdatesEnabled(True)
        self._loading_values=True
        self._model_index=min(max(previous[0],0),len(self.session.models)-1) if same else 0
        self._loading_values=False;self._model_changed()
        if same:
            for i in range(self.sequence_box.count()):
                if self.sequence_box.itemData(i)==previous[1]:self.sequence_box.setCurrentIndex(i);break
            self._elapsed_seconds=previous[3] if self.loop_check.isChecked() else min(previous[3],self._playback_end())
            if cameras is not None:
                self.viewport._camera_reference=cameras[0]
                self.official_viewport._camera_reference=cameras[1]
            self._refresh_frame()
        self.audio.set_source(self.session,self._previews)
        self.set_busy(self._busy)

    @property
    def suspended(self):
        return self.session is not None and self._loaded_models is None

    def suspend_preview(self):
        """Retain metadata/drafts, retire GL on GUI, defer CPU destruction."""
        self.continuous.exit(refresh=False)
        self.commit_fps_edit();self.pause()
        owners,loaders=self.audio.detach_source()
        owners.extend((self._previews,self._materials,self._arms_asset,
            self._original_asset,self._original_assets))
        if not self.suspended:
            self._suspended_cameras=tuple(dict(view._camera_reference) if view._camera_reference is not None else None
                for view in (self.viewport,self.official_viewport))
        for view in (self.viewport,self.official_viewport):
            view.setUpdatesEnabled(False)
            owners.extend((view.model,view.materials,view._pose))
            view.set_model(None,());view.release_resources()
        if self.session is not None:
            owners.append(self.session._preview_models)
            self.session._preview_models={}
        self._previews=[];self._materials=[];self._loaded_models=None
        self._arms_asset=None;self._original_asset=None;self._original_assets={}
        self._original_key=None;self._original_pending=None;self._original_error=None
        self.arms_label.setText(tr('未挂载额外手臂'))
        self.official_label.setText(tr('原版 V 模（预览已卸载）'))
        self.info_label.setText(tr('预览已卸载；重新进入动作矫正时载入。'))
        self._update_controls()
        return owners,loaders

    def clear_loaded(self):
        """A failed request for another result cannot expose the previous result."""
        self.continuous.exit(refresh=False)
        self.pause();self._loading_values=True
        self.viewport.setUpdatesEnabled(True);self.official_viewport.setUpdatesEnabled(True)
        self._suspended_cameras=None
        self.audio.set_source(None,())
        self.session=None;self._previews=[];self._materials=[];self._loaded_models=None
        self._arms_asset=None;self._reference_details.clear();self._warnings=[];self._detail_lines=[]
        self._frame=0.;self._elapsed_seconds=0.;self.log_card.messages.clear();self.details_text.clear()
        self._original_asset=None;self._original_key=None;self._original_pending=None;self._original_error=None
        self._original_assets={}
        self.official_viewport.set_model(None,());self.official_label.setText(tr('原版 V 模'))
        self._model_index=0;self.weapon_name_label.setText(tr('未载入武器'));self.model_switch_button.hide()
        self.sequence_box.clear();self.frame_slider.setRange(0,0)
        self.fps_input.setValue(30)
        self.viewport.set_model(None,());self.arms_label.setText(tr('未挂载额外手臂'))
        self.info_label.setText(tr('正在载入成品 V 模…'));self.timing_label.clear()
        self.frame_label.setText(tr('帧 0 / 0'))
        self.comparison.set_times(None,0,None,1,0)
        self._refresh_preview_caption()
        self._loading_values=False;self._update_controls()

    def _model_changed(self,*_):
        self.continuous.exit(refresh=False)
        if self._loading_values or not self.session or self.suspended:return
        self.pause();self._loading_values=True
        index=self._model_index
        from ..weapon_catalog import get_weapon
        def label(model):
            return tr('小手枪（双持）') if model.profile_id=='dual_pistol' else tr('小手枪（单持）') if model.profile_id=='pistol' else get_weapon(model.profile_id).name
        self.weapon_name_label.setText(display_text(label(self.session.models[index])))
        self.weapon_name_label.setToolTip(str(self.session.models[index].model_path))
        self.model_switch_button.setVisible(len(self.session.models)>1)
        if len(self.session.models)>1:
            self.model_switch_button.setText(tr('切换为{weapon}',weapon=display_text(label(self.session.models[(index+1)%len(self.session.models)]))))
        asset=self._original_assets.get(index)
        if asset is not None and not isinstance(asset,str):
            if self._original_key!=self._comparison_key():self.official_viewport.set_model(None,())
            self._original_asset=asset;self._original_key=self._comparison_key();self._original_error=None
            if self.compare_check.isChecked():self._apply_original_model()
        else:
            self._original_asset=None;self._original_key=None
            self._original_error=(self._comparison_key(),asset) if isinstance(asset,str) else None
            self.official_viewport.set_model(None,())
        self.sequence_box.clear()
        for action in self.session.models[index].actions:
            if (index,action.index) not in self.session.removed_sequences:
                self.sequence_box.addItem(sequence_option_text(action),userData=action.index)
        self._warnings=list(self._previews[index].warnings)
        self._apply_render_model()
        self._loading_values=False;self._sequence_changed()
        if self.compare_check.isChecked():QTimer.singleShot(0,self._ensure_original)

    def _sequence_changed(self,*_):
        if self._loading_values or self.suspended:return
        if self.continuous.active:return
        self.pause();action=self.current_action()
        self._frame=0.;self._elapsed_seconds=0.
        if action is None:
            self.info_label.setText(tr('没有已确认可播放且带 Activity 的动作。'))
            self._refresh_preview_caption()
            self.frame_slider.setRange(0,0);self._update_controls();return
        self._loading_values=True
        self.sequence_box.setToolTip(sequence_option_text(action))
        self.fps_input.setValue(self.effective_fps)
        timeline=self.current_timeline()
        self.frame_slider.setRange(0,max(timeline.frame_count-1,0));self.frame_slider.setValue(0)
        count=len(action.smd_frames)
        if not count:
            sequence=self._previews[self._model_index].data.theSequenceDescs[
                self.session.source_index(self._model_index,action.index)]
            count=len(sequence.theAnimDescIndexes or [])
        self.fps_label.setText(tr('主引用 FPS') if count>1 else 'FPS')
        self._loading_values=False
        from ..action_rules import _QcDocument
        model=self.session.models[self._model_index]
        compiled_only=getattr(self.session,'frames_source','')=='compiled'
        self._detail_lines=[display_text(action.label),'Activity: '+action.activity,tr('模型: {path}',path=str(model.model_path))]
        if compiled_only:
            self._detail_lines.append(tr('当前帧数来自编译模型；保存时反编译并执行 Subtract 修正，再修改 QC。'))
        else:self._detail_lines.append('QC: '+str(model.qc_path))
        try:
            if not compiled_only and model.qc_path not in self._reference_details:
                document=_QcDocument(model.qc_path)
                self._reference_details[model.qc_path]={i:tuple(
                    tr('引用 {slot}: {name}；SMD {smd_count} 帧；有效 {frame_count} 帧；原 FPS {fps}',
                       slot=slot,name=ref.token.value,smd_count=ref.smd_count,frame_count=ref.frame_count,fps=fps_text(ref.fps))
                    for slot,ref in enumerate(document.references(sequence)))
                    for i,sequence in enumerate(document.sequences)}
            if not compiled_only:
                source=self.session.source_index(self._model_index,action.index)
                self._detail_lines.extend(self._reference_details[model.qc_path][source])
        except (ValueError,OSError) as error:
            self._warnings.append(tr('QC 引用详情无法刷新: {error}',error=display_text(str(error))))
        self._detail_lines.append(tr('FPS 调整保留事件帧号；序列调换保留目标槽位并交换整套动作参数。'))
        self._camera_changed();self._refresh_details()

    def _camera_changed(self,*_):
        self.viewport.set_camera_mode(self.camera_box.currentData())
        self.official_viewport.set_camera_mode(self.camera_box.currentData())
        self._refresh_frame()

    def _fps_changed(self,value):
        if self.continuous.active:return
        if self._loading_values or not self.current_action():return
        if self._loaded_models is not self.session.models:return
        self.audio.stop()
        action=self.current_action()
        # Restoring the displayed baseline restores its complete accepted value,
        # not the spin box's decimal-rounded approximation.
        original_display=round_fps(action.fps)
        rate=action.fps if value==original_display else value
        self.session.set_fps(self._model_index,action.index,rate)
        self._elapsed_seconds=self._seconds_from_frame()
        self._refresh_frame();self._update_controls()

    def _seek(self,value):
        if self._loading_values:return
        if self.continuous.active:
            self.continuous.seek(value)
            self._last_tick=time.monotonic();self._refresh_frame();return
        self.audio.stop()
        self._elapsed_seconds=float(value)*self._frame_step_seconds()
        self._last_tick=time.monotonic();self._refresh_frame()

    def _step(self,offset):
        if self.continuous.active:
            self.pause();self.continuous.step(offset);return
        self.pause();self.frame_slider.setValue(self.frame_slider.value()+offset)

    def _refresh_preview_caption(self):
        action=self.current_action()
        caption=tr('当前 V 模 · $sequence {name}',name=action.name) if action else tr('当前 V 模 · 未选择动作')
        self.current_label.setText(caption);self.current_label.setToolTip(caption)
        self.current_fps_label.setText(tr('FPS：{fps}',fps=fps_text(self.fps_input.value())) if action else tr('FPS：—'))
        note=tr('预览动作速度不一定完全匹配游戏内动作速度，仅供参考。')
        if action is not None and not self.continuous.active:
            from ..weapon_ads import is_ads_or_mixed_activity
            activity=action.activity.upper()
            if (is_ads_or_mixed_activity(activity)
                    and activity.partition('_VM_')[2] in ('IDLE_TO_LOWERED','LOWERED_TO_IDLE')):
                note+='\n'+tr('当前仅预览单个序列。ADS 进入/退出的游戏效果还依赖运行时的 idle 状态切换及淡入淡出，单独播放时可能静止。')
        self.preview_note_label.setText(note)

    def _refresh_frame(self):
        if self.continuous.active:
            self.continuous.refresh();return
        self._refresh_preview_caption()
        action=self.current_action()
        if action is None or self.suspended:return
        timeline=self.current_timeline()
        duration=timeline.duration
        seconds=self._elapsed_seconds
        if self.loop_check.isChecked() and duration>0:seconds%=duration
        self._frame=min(seconds/duration,1.)*max(timeline.frame_count-1,0) if duration>0 else 0.
        self._sync_playback_slider()
        raw=', '.join(str(n) for n in action.smd_frames or (action.frame_count,))
        source=tr('编译主动画帧数') if getattr(self.session,'frames_source','')=='compiled' else tr('SMD 实际帧数')
        self.info_label.setText(tr('{source}：{raw}    序列帧数：{frames}    序列 FPS：{fps}    持续时间：{duration:.2f} 秒',
            source=source,raw=raw,frames=timeline.frame_count,fps=fps_text(timeline.fps),duration=duration)+(tr('（一轮）') if action.loop else ''))
        self.timing_label.setText(display_text(action.timing_note))
        self.frame_label.setText(tr('帧 {frame} / {last}',frame=int(self._frame),last=max(timeline.frame_count-1,0))
                                +(tr('  共用时间 {seconds:.2f} 秒',seconds=self.playhead_seconds) if self.compare_check.isChecked() else ''))
        extent=self._playback_end()
        cursor=self.playhead_seconds%extent if self.loop_check.isChecked() and extent>0 else self.playhead_seconds
        official=self._official_clock()
        official_seconds=self.playhead_seconds
        if self.loop_check.isChecked() and official is not None and official.duration>0:
            official_seconds%=official.duration
        note=tr('无匹配原版动画') if self._original_key==self._comparison_key() else tr('未载入原版动画')
        if self._original_error:note=tr('原版动画载入失败，详见模型与引用详情')
        self.comparison.set_times(official.duration if official else None,duration,
            official.frame_count if official else None,timeline.frame_count,cursor,
            official_seconds=official_seconds,model_seconds=seconds,official_note=note)
        self.viewport.set_frame(self.session.source_index(self._model_index,action.index),
                                self._frame,frame_span=max(timeline.frame_count-1,0))
        self._refresh_original()
        self._update_controls()

    def _play_pause(self):
        if self._timer.isActive():self.pause()
        elif self.continuous.active:
            if not self.loop_check.isChecked() and self._elapsed_seconds>=self._playback_end():
                self._elapsed_seconds=0.;self._refresh_frame()
            self._last_tick=time.monotonic();self._timer.start();self.play_button.setText(tr('暂停'))
        elif self.current_action() and self._playback_end()>0:
            if self.audio.enabled and self.audio.loading:return
            self.audio.stop()
            if not self.loop_check.isChecked() and self._elapsed_seconds>=self._playback_end():
                self._frame=0.;self._elapsed_seconds=0.;self.frame_slider.setValue(0)
            if self._elapsed_seconds==0:self._advance_audio(0,include_start=True)
            self._last_tick=time.monotonic();self._timer.start();self.play_button.setText(tr('暂停'))

    def _tick(self):
        if self.continuous.active:
            if self._busy:self.pause();return
            now=time.monotonic();self._elapsed_seconds+=max(now-self._last_tick,0.);self._last_tick=now
            if not self.loop_check.isChecked() and self._elapsed_seconds>=self._playback_end():
                self._elapsed_seconds=self._playback_end();self.pause()
            self._refresh_frame();return
        action=self.current_action()
        if action is None or self._busy:self.pause();return
        timeline=self.current_timeline()
        end=max(timeline.frame_count-1,0)
        playback_end=self._playback_end()
        if playback_end<=0:self.pause();return
        previous=self._elapsed_seconds
        now=time.monotonic();self._elapsed_seconds+=max(now-self._last_tick,0.);self._last_tick=now
        finished=not self.loop_check.isChecked() and self._elapsed_seconds>=playback_end
        if finished:self._elapsed_seconds=playback_end
        self._advance_audio(previous)
        if finished:self.pause(stop_audio=False)
        seconds=self._elapsed_seconds
        if self.loop_check.isChecked() and timeline.duration>0:seconds%=timeline.duration
        self._frame=min(seconds/timeline.duration,1.)*end if timeline.duration>0 else 0.
        self._refresh_frame()

    def pause(self,*,stop_audio=True):
        self._timer.stop();self.play_button.setText(tr('播放'))
        if stop_audio:self.audio.stop()

    def _back(self):
        if self._busy:return
        self.commit_fps_edit()
        self.pause();self.back_requested.emit()

    def _confirm_save(self):
        if self.continuous.active:return
        if self._busy or not self.session:return
        self.commit_fps_edit()
        if not self.session.changes:return
        self.pause()
        box=ActionFormDialog(tr('确认保存动作矫正'),self,confirm_text=tr('保存'),width=700)
        content=BodyLabel(tr('以下动作改动将先在暂存目录反编译并执行 Subtract 修正，再导出编译模型及必要伴随文件：')
                    if getattr(self.session,'standalone',False) else
                    tr('以下序列调换、剔除和 FPS 改动将覆盖本次转模的成品 QC 和 VPK：'),box.widget)
        content.setWordWrap(True);box.viewLayout.addWidget(content)
        details=TextEdit(box.widget);details.setReadOnly(True);details.setPlainText(confirmation_text(self.session))
        details.setMaximumHeight(240);box.viewLayout.addWidget(details)
        box.yesButton.setAutoDefault(False);box.cancelButton.setDefault(True);box.cancelButton.setFocus()
        if box.exec()==QDialog.Accepted:self.save_requested.emit()

    def _viewport_warning(self,message):
        if message and not any(display_text(existing)==display_text(message) for existing in self._warnings):
            self._warnings.append(message);self._refresh_details()

    def _refresh_details(self):
        lines=self._detail_lines+([tr('预览提示：')]+list(dict.fromkeys(self._warnings)) if self._warnings else [])
        if self._original_error:lines+=[tr('原版动画载入失败：{error}',error=display_text(self._original_error[1]))]
        if self.audio.notes:lines+=[tr('音效预览提示：')]+list(self.audio.notes)
        if self.log_card.messages:lines+=[tr('任务日志：')]+self.log_card.messages
        self.details_text.setPlainText('\n'.join(display_text(line) for line in lines))
        if not self._busy:
            count=len(set(self._warnings))
            self.status_label.setText(tr('{count} 项预览提示，详见“模型与引用详情”。',count=count) if count else '')

    def _update_controls(self):
        available=self.current_action() is not None and self._loaded_models is self.session.models
        for widget in (self.sequence_box,self.fps_input,self.frame_slider,self.play_button,
                       self.previous_button,self.next_button,self.loop_check,self.sound_check):
            widget.setEnabled(not self._busy and available)
        if self.audio.enabled and self.audio.loading:self.play_button.setEnabled(False)
        self.model_switch_button.setEnabled(not self._busy and self.session is not None and self._loaded_models is self.session.models)
        self.camera_box.setEnabled(not self._busy and available)
        self.fov_slider.setEnabled(not self._busy and available)
        self.compare_check.setEnabled(not self._busy and available)
        self.open_model_button.setEnabled(not self._busy)
        loading_arms=self._arms_worker is not None
        self.arms_button.setEnabled(not self._busy and not loading_arms and available)
        self.remove_arms_button.setEnabled(not self._busy and not loading_arms and self._arms_asset is not None)
        self.swap_button.setEnabled(not self._busy and not loading_arms and self.session is not None
                                   and self._loaded_models is self.session.models
                                   and len(self.session.models[self._model_index].swap_actions)>1)
        self.swap_list_button.setEnabled(not self._busy and self.session is not None
                                        and 0<=self._model_index<len(self.session.models)
                                        and bool(self.session.models[self._model_index].swap_actions
                                                 or self.session.models[self._model_index].actions))
        self.removal_button.setEnabled(not self._busy and not loading_arms and self.session is not None
            and self._loaded_models is self.session.models
            and any(s.removable for s in self.session.models[self._model_index].sequence_slots))
        self.back_button.setEnabled(not self._busy)
        dirty=self.session is not None and (bool(self.session.changes) or available and self.fps_input.has_pending_text)
        self.save_button.setEnabled(not self._busy and not loading_arms and bool(dirty))
        self.cancel_button.setVisible(self._busy);self.cancel_button.setEnabled(self._busy)
        self.continuous.update_controls(available)

    def _toggle_details(self):
        visible=self.details_text.isHidden()
        self._set_details_visible(visible)
        if self._config is not None:self._config.set(self._config.action_details_open,visible)

    def _set_details_visible(self,visible):
        self.viewport.setMinimumHeight(110 if visible else 170)
        self.details_text.setMaximumHeight(90)
        self.details_text.setVisible(visible)

    def set_busy(self,busy):
        self._busy=bool(busy)
        if busy:self.pause()
        self._update_controls()
        if not busy and self.compare_check.isChecked():QTimer.singleShot(0,self._ensure_original)

    def finish_worker(self,summary):
        self.pause()
        count=len(set(self._warnings))
        self.status_label.setText(display_text(summary)+(tr(' {count} 项预览提示，详见模型与引用详情。',count=count) if count else ''))

    def hideEvent(self,event):
        self.pause();super().hideEvent(event)
