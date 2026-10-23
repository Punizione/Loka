"""Independent firearm and ordinary-melee VPK conversion page."""
from pathlib import Path
import re
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QGridLayout
from qfluentwidgets import (BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox,
                           IndeterminateProgressBar, LineEdit, PrimaryPushButton,
                           PushButton, ProgressBar, ScrollArea, SubtitleLabel, TextEdit,
                           InfoBar, InfoBarPosition, SpinBox)

from ..weapon_catalog import TARGET_WEAPONS, allowed_targets, SHOTGUN_IDS
from ..i18n import tr, display_text
from ..weapon_conversion import (PackageInspection, plan_output, UnsupportedViewModelError,
                                 UNSUPPORTED_VIEW_MODEL_MESSAGE)


IK_HANDLING_LABELS={"default":"默认","compatible":"单步源采样保留","delete":"删除","usesource":"使用源动画 (UseSource)"}


class ConversionLogCard(CardWidget):
    def __init__(self, advanced, parent=None):
        super().__init__(parent)
        self.advanced = advanced
        self.messages = []
        layout = QVBoxLayout(self)
        layout.addWidget(SubtitleLabel(tr('当前处理状态'), self))
        self.text = TextEdit(self)
        self.text.setReadOnly(True)
        self.text.setFixedHeight(190)
        layout.addWidget(self.text)

    def append(self, message):
        self.messages.append(message)
        self.refresh()

    def clear(self):
        self.messages.clear()
        self.text.clear()

    def refresh(self):
        if self.advanced():
            content = '\n'.join(display_text(message) for message in self.messages)
        else:
            content = display_text(next((m for m in reversed(self.messages) if not m.startswith('[详情]')), ''))
        self.text.setPlainText(content)
        scroll = self.text.verticalScrollBar()
        scroll.setValue(scroll.maximum())


class WeaponConversionInterface(ScrollArea):
    def __init__(self, advanced, parent=None, *, config=None):
        super().__init__(parent)
        self.package: PackageInspection | None = None
        self._busy = False
        self._conversion_options = None
        self.correction_result = None
        self.correction_options = None
        self._conversion_identity = None
        self._advanced=advanced
        self._scan_generation=0
        self._pending_scan=None
        self._scan_timer=QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.timeout.connect(self.dispatch_pending_scan)
        self.scrollWidget = CardWidget(self)
        layout = QVBoxLayout(self.scrollWidget)
        layout.setContentsMargins(32,32,32,32)
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignTop)
        layout.addWidget(SubtitleLabel(tr('武器转模'), self))
        note = CaptionLabel(tr('支持枪械互转和近战互转，处理动作、声音、ADS 和材质/HUD，编译验证后生成同级 VPK。原始解包与待打包内容独立保存。开启保留临时文件可查看转换源、编译日志与验证记录。'), self)
        note.setWordWrap(True)
        layout.addWidget(note)

        card = CardWidget(self)
        fields = QVBoxLayout(card)
        fields.setContentsMargins(18,14,18,14)
        fields.addWidget(BodyLabel(tr('原始 VPK'),card))
        row = QHBoxLayout()
        self.input_edit = LineEdit(card)
        self.input_edit.setPlaceholderText(tr('拖入 .vpk 文件，或浏览选择'))
        row.addWidget(self.input_edit,1)
        self.browse_button = PushButton(tr('浏览'),card)
        self.browse_button.clicked.connect(self._browse)
        row.addWidget(self.browse_button)
        fields.addLayout(row)
        self.source_label = BodyLabel(tr('来源武器：未解析'),card)
        fields.addWidget(self.source_label)
        self.source_pistol_label=BodyLabel(tr('手枪模型选择'),card)
        self.source_pistol_box=ComboBox(card)
        fields.addWidget(self.source_pistol_label);fields.addWidget(self.source_pistol_box)
        self._preferred_source_pistol_mode='single'
        self.resolved_source_view_model=None
        self.source_pistol_label.hide();self.source_pistol_box.hide()
        fields.addWidget(BodyLabel(tr('目标武器'),card))
        self.target_box = ComboBox(card)
        fields.addWidget(self.target_box)
        self.reload_loop_label=BodyLabel(tr('换弹 Loop 次数'),card)
        self.reload_loop_box=SpinBox(card);self.reload_loop_box.setRange(0,999);self.reload_loop_box.setValue(1)
        self.reload_loop_box.setToolTip(tr('霰弹枪转其他枪械时，换弹循环重复次数；0 表示仅拼接起始和结束。总时长继续适配目标武器。'))
        fields.addWidget(self.reload_loop_label);fields.addWidget(self.reload_loop_box)
        self.reload_loop_label.hide();self.reload_loop_box.hide()
        self.reload_loop_box.valueChanged.connect(self.invalidate_correction)
        fields.addWidget(BodyLabel(tr('最终 VPK 名称（留空使用默认名称）'),card))
        self.output_edit = LineEdit(card)
        self.output_edit.setPlaceholderText(tr('例如 my_weapon_rifle.vpk'))
        fields.addWidget(self.output_edit)
        self.output_label = CaptionLabel(tr('最终输出：请选择来源 VPK 和目标武器'),card)
        self.output_label.setWordWrap(True)
        fields.addWidget(self.output_label)
        layout.addWidget(card)

        option_card = self.option_card = CardWidget(self)
        options = QVBoxLayout(option_card)
        options.setContentsMargins(16,12,16,12)
        options.addWidget(BodyLabel(tr('处理选项'),option_card))
        self.material_check = CheckBox(tr('材质隔离'),option_card)
        self.material_check.setToolTip(tr('为材质分配独立目录，减少与其他MOD的材质冲突。'))
        self.ads_check = CheckBox(tr('保留ADS(含MIXED)'),option_card)
        self.ads_check.setToolTip(tr('保留MOD中的ADS及MIXED动画；关闭后移除这些动作和层引用。'))
        self.keep_temp_check = CheckBox(tr('保留临时文件'),option_card)
        self.keep_temp_check.setToolTip(tr('转换后保留解包、反编译和编译文件，便于检查结果。'))
        self.l4n_check = CheckBox(tr('过剩音频转为 L4N 音频'),option_card)
        self.l4n_check.setToolTip(tr('将目标音频槽位容纳不下的音效，另存为L4N音频副包。'))
        self.normalize_vmt_check = CheckBox(tr('VMT规范化'),option_card)
        self.normalize_vmt_check.setToolTip(tr('检查材质文件，修复可确定的VMT语法问题。'))
        self.fps_check = CheckBox(tr('FPS匹配'),option_card)
        self.fps_check.setToolTip(tr('优先使用MOD中的FPS进行换算'))
        self.inspect_fps_check = CheckBox(tr('检视FPS排除'),option_card)
        self.inspect_fps_check.setToolTip(tr('helping_hand、item 系列动作保留原模 FPS，不进行时长换算或 FPS 匹配。'))
        self.ads_check.setChecked(True)
        self.fadein_check=CheckBox(tr('推击淡入'),option_card)
        self.fadein_check.setChecked(False)
        self.fadein_check.setToolTip(tr('为普通、15连及ADS/MIXED推击动画强制设置0.2秒淡入。'))
        grid=QGridLayout();grid.setHorizontalSpacing(12);grid.setVerticalSpacing(8)
        for box,row,column in ((self.material_check,0,0),(self.normalize_vmt_check,0,1),(self.ads_check,0,2),
                               (self.l4n_check,1,0),(self.fadein_check,2,0),
                               (self.fps_check,2,1),(self.inspect_fps_check,2,2),(self.keep_temp_check,3,0)):
            box.setMinimumWidth(box.fontMetrics().horizontalAdvance(box.text())+28)
            grid.addWidget(box,row,column)
        self.audio_logic_box=ComboBox(option_card)
        for label,value in ((tr('频率优先'),'frequency'),(tr('均衡匹配'),'balanced'),(tr('完整优先'),'complete')):
            self.audio_logic_box.addItem(tr(label),userData=value)
        self.audio_logic_box.setToolTip(tr('频率优先：优先保留常用音效。\n均衡匹配：保留常用音效并优化时长匹配。\n完整优先：优先减少截断。'))
        self.ik_box=ComboBox(option_card)
        for value,label in IK_HANDLING_LABELS.items():self.ik_box.addItem(tr(label),userData=value)
        self.ik_box.setToolTip(tr('默认：保留原有IK规则。\n删除：移除IK规则。\n使用源动画：根据动画重新计算接触IK。'))
        self.ik_box.setCurrentIndex(1)
        self.audio_logic_box.setMinimumWidth(140)
        self.ik_box.setMinimumWidth(max(self.ik_box.fontMetrics().horizontalAdvance(tr(label)) for label in IK_HANDLING_LABELS.values())+52)
        for column,(label,combo) in enumerate(((tr('音频处理逻辑'),self.audio_logic_box),(tr('IK 处理'),self.ik_box)),1):
            row=QVBoxLayout();row.setSpacing(4)
            caption=BodyLabel(tr(label),option_card);caption.setToolTip(combo.toolTip())
            row.addWidget(caption);row.addWidget(combo)
            grid.addLayout(row,1,column)
        for column in range(3):
            grid.setColumnStretch(column,1)
        options.addLayout(grid)
        layout.addWidget(option_card)

        actions = QHBoxLayout()
        self.run_button = PrimaryPushButton(tr('开始转模'),self)
        self.run_button.clicked.connect(lambda:self.window().run_weapon_conversion(self))
        actions.addWidget(self.run_button)
        self.cancel_button = PushButton(tr('取消'),self)
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.setEnabled(False)
        actions.addWidget(self.cancel_button)
        self.correction_button=PushButton(tr('动作矫正'),self)
        self.correction_button.clicked.connect(lambda:self.window().open_action_correction(self))
        self.correction_button.setEnabled(False)
        actions.addWidget(self.correction_button)
        self.progress = IndeterminateProgressBar(self)
        self.progress.hide()
        layout.addLayout(actions)
        layout.addWidget(self.progress)
        self.stage_panel=CardWidget(self.scrollWidget)
        stages=QVBoxLayout(self.stage_panel);stages.setContentsMargins(0,8,0,8)
        self.stage_label=CaptionLabel(tr('准备转换'),self.stage_panel);self.stage_label.setWordWrap(True)
        self.stage_progress=ProgressBar(self.stage_panel);self.stage_progress.setRange(0,100);self.stage_progress.setValue(0)
        stages.addWidget(self.stage_label);stages.addWidget(self.stage_progress)
        layout.addWidget(self.stage_panel);self.stage_panel.hide()
        self._conversion_active=False;self._result_seen=False;self._completed_units=0
        self.log_card = ConversionLogCard(advanced,self)
        layout.addWidget(self.log_card)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setObjectName('weaponConversionInterface')
        self.enableTransparentBackground()
        self.input_edit.textChanged.connect(self._input_changed)
        self.target_box.currentIndexChanged.connect(self.update_preview)
        self.target_box.currentIndexChanged.connect(self.invalidate_correction)
        self.source_pistol_box.currentIndexChanged.connect(self._source_pistol_changed)
        self.output_edit.textChanged.connect(self.update_preview)
        self.output_edit.textChanged.connect(self.invalidate_correction)
        self._fill_targets()
        if config is not None:self._bind_processing_options(config)
        self.set_advanced(bool(self._advanced()))
        self.set_busy(False)

    def _bind_processing_options(self, config):
        # Restore before connecting signals so opening the page does not save.
        self._source_config=config
        self._preferred_source_pistol_mode=config.get(config.conversion_source_pistol_mode)
        for box,item in ((self.material_check,config.conversion_isolate_materials),
                         (self.normalize_vmt_check,config.conversion_normalize_vmt),
                         (self.ads_check,config.conversion_keep_ads),
                         (self.keep_temp_check,config.conversion_keep_temp),
                         (self.l4n_check,config.conversion_l4n),
                         (self.fps_check,config.conversion_fps_match),
                         (self.inspect_fps_check,config.conversion_exclude_inspect_fps),
                         (self.fadein_check,config.conversion_melee_layer_fadein_fix)):
            box.setChecked(config.get(item))
            box.toggled.connect(lambda checked,setting=item:config.set(setting,checked))
        for combo,item in ((self.audio_logic_box,config.conversion_audio_logic),
                           (self.ik_box,config.conversion_ik_rule_handling)):
            combo.setCurrentIndex(next(i for i in range(combo.count()) if combo.itemData(i)==config.get(item)))
            combo.currentIndexChanged.connect(
                lambda index,box=combo,setting=item:config.set(setting,box.itemData(index)))

    def _fit_option_labels(self):
        changed=False
        for box in (self.material_check,self.normalize_vmt_check,self.ads_check,self.keep_temp_check,self.l4n_check,self.fadein_check,self.fps_check,self.inspect_fps_check):
            box.ensurePolished();width=box.fontMetrics().horizontalAdvance(box.text())+28
            if box.minimumWidth()!=width:box.setMinimumWidth(width);changed=True
        self.ik_box.ensurePolished()
        width=max(self.ik_box.fontMetrics().horizontalAdvance(tr(label)) for label in IK_HANDLING_LABELS.values())+52
        if self.ik_box.minimumWidth()!=width:self.ik_box.setMinimumWidth(width);changed=True
        if changed:self.scrollWidget.layout().activate()

    def showEvent(self,event):
        super().showEvent(event)
        self._fit_option_labels()
        QTimer.singleShot(0,self._fit_option_labels)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'ik_box'):QTimer.singleShot(0,self._fit_option_labels)

    def _browse(self):
        path,_ = QFileDialog.getOpenFileName(self,tr('选择武器 VPK'),'','VPK (*.vpk)')
        if path:self.set_input_path(path)

    def _cancel(self):
        self.window()._cancel_event.set()
        self.log_card.append(tr('已请求取消，将在当前操作结束后清理。'))

    def set_input_path(self, path):
        self.input_edit.setText(path)
        self.invalidate_package()
        self.request_scan(immediate=True)

    def _input_changed(self):
        self.invalidate_package()
        self.request_scan()

    def invalidate_package(self):
        self.invalidate_correction()
        self.package = None
        self._fill_source_pistol_modes()
        self.source_label.setText(tr('来源武器：未解析'))
        self._fill_targets()
        self.update_preview()

    def input_path(self):
        value=self.input_edit.text().strip()
        if not value:return None
        try:
            return Path(value).resolve()
        except (OSError,ValueError):return None

    def scan_path(self):
        path=self.input_path()
        try:return path if path is not None and path.suffix.lower()=='.vpk' and path.is_file() else None
        except OSError:return None

    def scan_is_current(self,request):
        return request is not None and request==(self._scan_generation,self.input_path())

    def request_scan(self,*,immediate=False):
        self._scan_generation+=1
        path=self.scan_path()
        self._pending_scan=(self._scan_generation,path) if path is not None else None
        self._scan_timer.stop()
        if self._pending_scan is not None:self._scan_timer.start(0 if immediate else 180)

    def cancel_pending_scan(self):
        self._scan_timer.stop();self._pending_scan=None

    def dispatch_pending_scan(self):
        request=self._pending_scan
        if request is None:return
        if not self.scan_is_current(request):self.cancel_pending_scan();return
        if self.window().run_weapon_scan(self,request):self.cancel_pending_scan()
        else:self._scan_timer.start(30)

    def on_scan_outcome(self,outcome):
        request,package,error=outcome
        if not self.scan_is_current(request):return
        if error:
            self.invalidate_package()
            if isinstance(error,UnsupportedViewModelError):
                self._show_unsupported_notice()
        elif package is not None:
            self.set_package(package)
            self.log_card.append(f'[详情] {package.entry_count} 个文件；{len(package.view_models)} 个 v 模型')

    def _fill_targets(self):
        previous = self.target_box.currentData()
        self.target_box.blockSignals(True)
        self.target_box.clear()
        self.target_box.addItem(tr('请选择目标武器'),userData=None)
        for weapon in (allowed_targets(self.package.weapon.id) if self.package else TARGET_WEAPONS):
            self.target_box.addItem(tr(weapon.name),userData=weapon.id)
        for i in range(self.target_box.count()):
            if self.target_box.itemData(i) == previous:
                self.target_box.setCurrentIndex(i)
                break
        self.target_box.blockSignals(False)

    def set_package(self, package):
        if self.input_path() != package.path:
            return
        self.package = package
        self._fill_source_pistol_modes()
        self._fill_targets()
        self.update_preview()

    def source_pistol_mode(self):
        return (self.source_pistol_box.currentData() or 'single') if self.package and self.package.weapon.id=='pistol' else 'single'

    def _fill_source_pistol_modes(self):
        modes=self.package.source_pistol_modes if self.package else ()
        self.source_pistol_box.blockSignals(True);self.source_pistol_box.clear()
        if modes:
            for mode,label in (('single',tr('单持')),('dual',tr('双持'))):
                if mode in modes:self.source_pistol_box.addItem(tr(label),userData=mode)
            preferred=self._preferred_source_pistol_mode
            self.source_pistol_box.setCurrentIndex(next((i for i in range(self.source_pistol_box.count())
                if self.source_pistol_box.itemData(i)==preferred),0))
        self.source_pistol_box.blockSignals(False)
        self.source_pistol_label.setVisible(bool(modes));self.source_pistol_box.setVisible(bool(modes))
        self.resolved_source_view_model=None

    def _source_pistol_changed(self,*_):
        self.invalidate_correction()
        if not self.package:return
        self._preferred_source_pistol_mode=self.source_pistol_mode()
        if hasattr(self,'_source_config'):
            config=self._source_config;config.set(config.conversion_source_pistol_mode,self._preferred_source_pistol_mode)
        self.update_preview()

    def update_preview(self, *_):
        self._valid_output = False
        self.resolved_source_view_model=None
        if self.package:
            try:
                self.resolved_source_view_model=self.package.resolve_view_model(self.source_pistol_mode())
                model=tr('；模型：{path}', path=self.resolved_source_view_model) if self.package.weapon.id=='pistol' else ''
                self.source_label.setText(tr('来源武器：{name}{model}', name=tr(self.package.weapon.name), model=model))
            except ValueError as error:
                self.source_label.setText(display_text(str(error)));self.run_button.setEnabled(False);return
        target = self.target_box.currentData()
        compose=bool(self.package and self.package.weapon.id in SHOTGUN_IDS and target and target not in SHOTGUN_IDS)
        self.reload_loop_label.setVisible(compose);self.reload_loop_box.setVisible(compose)
        if self.package and target and self.scan_path()==self.package.path:
            try:
                output = plan_output(self.package.path,target,self.output_edit.text())
                self.output_label.setText(tr('最终输出：{path}', path=output))
                self._valid_output = True
                if output.exists():self.output_label.setText(tr('结果文件已存在，请更改名称：{path}', path=output))
            except ValueError as error:
                self.output_label.setText(display_text(str(error)))
        else:
            self.output_label.setText(tr('最终输出：请选择来源 VPK 和目标武器'))
        self.run_button.setEnabled(not self._busy and self._valid_output)

    def set_advanced(self,checked):
        for box in (self.keep_temp_check,self.fps_check,self.inspect_fps_check):
            box.setVisible(bool(checked))
            box.setEnabled(bool(checked) and not self._busy)

    def set_busy(self, busy):
        self._busy = busy
        for widget in (self.input_edit,self.browse_button,self.source_pistol_box,self.target_box,self.reload_loop_box,self.output_edit,self.keep_temp_check,self.ads_check,self.material_check,self.l4n_check,self.normalize_vmt_check,self.audio_logic_box,self.ik_box,self.fadein_check,self.fps_check,self.inspect_fps_check):
            widget.setEnabled(not busy)
        self.keep_temp_check.setEnabled(not busy and bool(self._advanced()))
        self.fps_check.setEnabled(not busy and bool(self._advanced()))
        self.inspect_fps_check.setEnabled(not busy and bool(self._advanced()))
        self.cancel_button.setEnabled(busy)
        self._refresh_correction_button()
        self.update_preview()

    def invalidate_correction(self,*_):
        self.correction_result=None
        self.correction_options=None
        self._refresh_correction_button()

    def _refresh_correction_button(self):
        result=self.correction_result
        available=(not self._busy and result is not None and self.correction_options is not None
                   and result.success and result.published and result.work_dir and result.output_path
                   and Path(result.work_dir).is_dir() and Path(result.output_path).is_file()
                   and self._conversion_identity==self._correction_identity())
        self.correction_button.setEnabled(bool(available))

    def begin_conversion(self,options=None):
        self.invalidate_correction()
        self._conversion_options=options
        self._conversion_identity=self._correction_identity()
        self._fit_option_labels()
        self._conversion_active=True;self._result_seen=False;self._completed_units=0;self._unsupported_notice_seen=False
        self.stage_progress.setValue(0);self.stage_label.setText(tr('准备转换：0/9 阶段已完成'))
        self.stage_panel.show()
        QTimer.singleShot(0,lambda:self.ensureWidgetVisible(self.stage_panel,0,12))

    def begin_scan(self):
        self._conversion_active=False;self._result_seen=False;self._unsupported_notice_seen=False;self.stage_panel.hide()

    def on_stage_progress(self,message):
        if not self._conversion_active:return
        match=re.match(r'^\[阶段\]\s+([1-9])/9(?:\s|$)',message)
        if not match:return
        completed=int(match[1])-1
        if completed<self._completed_units:return
        self._completed_units=completed
        self.stage_progress.setValue(completed*100//9)
        self.stage_label.setText(tr('{stage}；{count}/9 阶段已完成', stage=display_text(message.removeprefix('[阶段]').strip()), count=completed))

    def finish_worker(self,summary):
        if self._conversion_active and not self._result_seen:
            cancelled='取消' in summary
            self.stage_label.setText(tr('转换已取消') if cancelled else tr('转换失败'))
            self._conversion_active=False
            self._show_completion_notice(False,cancelled)

    def _show_completion_notice(self,success,cancelled=False):
        show=InfoBar.success if success else InfoBar.error
        title=tr('转模成功') if success else tr('转模已取消') if cancelled else tr('转模失败')
        content=tr('VPK 已生成。') if success else tr('操作已取消。') if cancelled else tr('请查看处理日志。')
        show(title,content,isClosable=False,duration=3000,
             position=InfoBarPosition.TOP_RIGHT,parent=self.window())

    def _show_unsupported_notice(self):
        if getattr(self,'_unsupported_notice_seen',False):return
        self._unsupported_notice_seen=True
        InfoBar.error(tr('转模已跳过'),display_text(UNSUPPORTED_VIEW_MODEL_MESSAGE),isClosable=False,duration=5000,
                      position=InfoBarPosition.TOP_RIGHT,parent=self.window())

    def set_result(self, result):
        options=self._conversion_options
        self.correction_result=None;self.correction_options=None
        if (options is not None and options.keep_temp and result.success and result.published
                and self._conversion_identity==self._correction_identity()
                and result.work_dir and result.output_path and Path(result.work_dir).is_dir()
                and Path(result.output_path).is_file() and result.converted_qc_files):
            self.correction_result=result
            self.correction_options=result.conversion_options or options
        self._refresh_correction_button()
        if self._conversion_active:
            self._result_seen=True;self._conversion_active=False
            if result.success and result.published:
                self.stage_progress.setValue(100);self.stage_label.setText(tr('转换完成：9/9 阶段已完成，VPK 已发布'))
            elif result.unsupported_view_model:self.stage_label.setText(tr('转换已跳过：不支持此类 MOD'))
            elif result.cancelled:self.stage_label.setText(tr('转换已取消'))
            elif result.success:self.stage_label.setText(tr('转换尚未发布'))
            else:self.stage_label.setText(tr('转换失败'))
            if result.unsupported_view_model:
                self.invalidate_package()
                self._show_unsupported_notice()
            else:self._show_completion_notice(result.success and result.published,result.cancelled)
        for warning in result.warnings:self.log_card.append('[详情] 警告：'+warning)
        if result.work_dir:self.log_card.append(tr('保留的临时文件：')+str(result.work_dir))

    def _correction_identity(self):
        return (self._scan_generation,self.input_path(),self.target_box.currentData(),
                self.output_edit.text(),self.source_pistol_mode(),self.reload_loop_box.value())
