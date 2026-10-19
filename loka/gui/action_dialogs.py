"""Fluent action-editor dialogs with explicit Chinese button text."""
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidgetItem
from qfluentwidgets import BodyLabel, MessageBoxBase, PushButton, SubtitleLabel, TableWidget

from ..fps_values import fps_text,round_fps


class ActionFormDialog(MessageBoxBase):
    def __init__(self,title,parent,*,confirm_text='确认',width=480):
        super().__init__(parent=parent.window() if parent is not None else None)
        self.titleLabel=SubtitleLabel(title,self.widget)
        self.viewLayout.addWidget(self.titleLabel)
        self.yesButton.setText(confirm_text);self.cancelButton.setText('取消')
        self.widget.setMinimumWidth(min(width,max(parent.window().width()-48,280)) if parent else width)
        self.validate_callback=None
        self._closing=False

    def validate(self):
        if self._closing:return False
        return bool(self.validate_callback()) if self.validate_callback is not None else True

    def done(self,code):
        # Fluent dialogs animate their close for 100 ms. Commit the choice once
        # so another click/Escape cannot reapply a swap or change that choice.
        if self._closing:return
        self._closing=True;self.buttonGroup.setEnabled(False)
        super().done(code)


class UnsavedActionDialog(ActionFormDialog):
    def __init__(self,parent):
        super().__init__('未保存的动作改动',parent,confirm_text='保存',width=520)
        content=BodyLabel('当前动作矫正有未保存的序列或 FPS 改动。是否保存？',self.widget)
        content.setWordWrap(True);self.viewLayout.addWidget(content)
        self.choice='cancel'
        self.discardButton=PushButton('不保存',self.buttonGroup)
        self.buttonLayout.insertWidget(1,self.discardButton,1)
        self.discardButton.clicked.connect(self._discard)
        self.yesButton.setAutoDefault(False);self.cancelButton.setDefault(True)
        self.cancelButton.setFocus()

    def validate(self):
        if self._closing:return False
        self.choice='save'
        return True

    def _discard(self):
        if self._closing:return
        self.choice='discard';self.accept()


class SequenceSwapComparisonDialog(ActionFormDialog):
    """Read-only final draft permutation; Activity belongs to the target slot."""
    def __init__(self,session,model_index,parent):
        super().__init__('序列调换列表对比',parent,confirm_text='关闭',width=1060)
        description=BodyLabel('左侧为当前成品，右侧为当前草稿的最终动作来源及 FPS。'
            '槽位名称、Activity 和顺位保持不变；保存后以新成品为基准。',self.widget)
        description.setWordWrap(True);self.viewLayout.addWidget(description)
        model=session.models[model_index]
        actions=sorted((a for a in model.swap_actions or model.actions if a.activity),key=lambda a:a.index)
        self.table=TableWidget(self.widget)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(['顺位','槽位 Activity','原动作','调换后动作','状态'])
        self.table.setRowCount(len(actions));self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setWordWrap(False)
        changed_font=self.table.font();changed_font.setBold(True)
        for row,original in enumerate(actions):
            source=session.baseline_action(model_index,session.source_index(model_index,original.index),all_slots=True)
            rate=session.effective_fps(model_index,original.index)
            changed=source.index!=original.index
            state='已调换' if changed else 'FPS 改动' if round_fps(rate)!=round_fps(original.fps) else '未改动'
            texts=(str(original.index),original.activity,
                   original.name+' · '+fps_text(original.fps)+' FPS',
                   source.name+' · '+fps_text(rate)+' FPS',state)
            for col,text in enumerate(texts):
                item=QTableWidgetItem(text);item.setToolTip(text)
                if changed:item.setFont(changed_font)
                self.table.setItem(row,col,item)
        header=self.table.horizontalHeader()
        for col in (0,1,4):header.setSectionResizeMode(col,QHeaderView.ResizeToContents)
        for col in (2,3):header.setSectionResizeMode(col,QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setFixedHeight(min(max(170,len(actions)*36+36),
                                      max(170,min(440,parent.window().height()-240))))
        self.viewLayout.addWidget(self.table)
        self.cancelButton.hide()
