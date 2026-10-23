"""Draft removal selection, labelled by destination slot and current payload."""
from PySide6.QtWidgets import QWidget,QVBoxLayout
from qfluentwidgets import BodyLabel,CaptionLabel,CheckBox,ScrollArea,PushButton
from .action_dialogs import ActionFormDialog
from ..i18n import tr,display_text


class SequenceRemovalDialog(ActionFormDialog):
    def __init__(self,page):
        super().__init__('序列剔除',page,width=850)
        self.page=page;self.checks={}
        note=BodyLabel(tr('仅可剔除带明确 Activity 的额外槽位；调换后按目标槽位判断。勾选后加入草稿，保存时从成品 QC 移除；取消勾选可恢复。'),self.widget)
        note.setWordWrap(True);self.viewLayout.addWidget(note)
        model_index=page._model_index;model=page.session.models[model_index]
        area=ScrollArea(self.widget);area.setWidgetResizable(True)
        content=QWidget();layout=QVBoxLayout(content)
        for slot in model.sequence_slots:
            if not slot.removable:continue
            donor=page.session.source_index(model_index,slot.index)
            source=model.sequence_slots[donor]
            label=tr('{name}[顺位：{index}] · {activity}；当前内容：{source}[原顺位：{donor}]',
                     name=slot.name,index=slot.index,activity=slot.activity or tr('无 Activity'),source=source.name,donor=donor)
            check=CheckBox(label,content);check.setToolTip(label)
            check.setChecked((model_index,slot.index) in page.session.removed_sequences)
            layout.addWidget(check);self.checks[slot.index]=check
        layout.addStretch();area.setWidget(content)
        area.setMinimumHeight(150);area.setMaximumHeight(360);self.viewLayout.addWidget(area)
        restore=PushButton(tr('恢复全部'),self.widget)
        restore.clicked.connect(lambda:[c.setChecked(False) for c in self.checks.values()])
        self.viewLayout.addWidget(restore)
        self.message=CaptionLabel('',self.widget);self.message.setWordWrap(True);self.viewLayout.addWidget(self.message)
        self.validate_callback=self.apply

    def apply(self):
        try:self.page.set_removed_sequences({i for i,c in self.checks.items() if c.isChecked()})
        except (ValueError,OSError) as error:self.message.setText(display_text(str(error)));return False
        return True
