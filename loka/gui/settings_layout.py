"""Content-sized settings groups; wrapped descriptions follow viewport width."""
from PySide6.QtWidgets import QWidget,QVBoxLayout,QSizePolicy
from PySide6.QtCore import Qt
from qfluentwidgets import SubtitleLabel,PushSettingCard


class SettingsGroup(QWidget):
    def __init__(self,title,parent=None):
        super().__init__(parent)
        self.vBoxLayout=QVBoxLayout(self)
        self.vBoxLayout.setContentsMargins(0,0,0,0);self.vBoxLayout.setSpacing(10)
        self.titleLabel=SubtitleLabel(title,self);self.vBoxLayout.addWidget(self.titleLabel)
        self.cardLayout=QVBoxLayout();self.cardLayout.setContentsMargins(0,0,0,0)
        self.cardLayout.setSpacing(2);self.vBoxLayout.addLayout(self.cardLayout)

    def addSettingCard(self,card):
        self.cardLayout.addWidget(card)


class SettingsPathCard(PushSettingCard):
    """Keep path/action buttons visible while long descriptions wrap."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.setMinimumHeight(70);self.setMaximumHeight(16777215)
        self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Minimum)
        self.hBoxLayout.setContentsMargins(16,12,0,12)
        self.hBoxLayout.setStretch(2,1)
        self.hBoxLayout.setStretch(4,0)
        self.hBoxLayout.setAlignment(Qt.AlignmentFlag(0))
        self.vBoxLayout.setAlignment(Qt.AlignmentFlag(0))
        for label in (self.titleLabel,self.contentLabel):
            label.setWordWrap(True);label.setMinimumWidth(0)
            policy=label.sizePolicy();policy.setHorizontalPolicy(QSizePolicy.Ignored)
            label.setSizePolicy(policy)
            self.vBoxLayout.setAlignment(label,Qt.AlignmentFlag(0))
