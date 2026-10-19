"""PySide6 + qfluentwidgets GUI (Fluent Design) for the Python MDL decompiler.

Layout: a ``FluentWindow`` with a fixed (non-collapsible) left navigation bar:

1. 首页            -- copyright / author information
2. 设置            -- tool paths (game folder, studiomdl.exe), persisted to disk
3. 武器转模        -- weapon conversion
4. 兼容ADS处理     -- advanced backup -> decompile -> compile
5. 反编译          -- input selection, file/text options, run button, log
6. Subtract修正    -- advanced delta subtract base table + per-animation tuning
7. BodyGroup处理   -- advanced mixed bodygroup event mapping

The theme toggle lives at the very bottom of the navigation bar; the app starts
in dark mode on first run and persists the chosen theme.  Shell drag & drop is
handled natively by Qt (see ``MainWindow.dropEvent``), so this module imports
neither Tk nor any TkDND extension.
"""

from __future__ import annotations

import os
import sys
import threading
import argparse
import warnings
import json
from dataclasses import asdict, replace
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal,QTimer, QRectF, QEvent, QRect
from PySide6.QtGui import (QDragEnterEvent, QDropEvent, QIcon, QPixmap, QResizeEvent,
                          QPainter, QPainterPath, QColor, QPen)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QAbstractItemView,
    QHeaderView,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    ConfigItem,
    ConfigValidator,
    OptionsValidator,
    QConfig,
    DoubleSpinBox,
    FluentIcon,
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    IndeterminateProgressBar,
    LineEdit,
    NavigationItemPosition,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    PushSettingCard,
    ScrollArea,
    SettingCardGroup,
    SubtitleLabel,
    SwitchSettingCard,
    TableWidget,
    TextEdit,
    Theme,
    qconfig,
    setTheme,
    setThemeColor,
    isDarkTheme,
)

from .. import __version__
from ..process_limits import exception_text
from ..core.bodygroup_events import DEFAULT_MIXED_SMD_NAMES, MIXED_ACTIVITIES
from ..decompiler import DecompileOptions, MdlDecompiler, detect_mdl_version  # noqa: F401
from ..mdl_info import MdlOverview, read_mdl_overview
from ..compile_game import is_compile_bundle
from ..one_click import find_studiomdl, run_one_click
from ..weapon_conversion import (ConversionOptions, inspect_weapon_package, convert_weapon_package,plan_output,
                                 UnsupportedViewModelError)
from .weapon_conversion import WeaponConversionInterface,IK_HANDLING_LABELS
from .startup_artwork import BACKGROUND_PIDS, PREVIEW_CHOICES, StartupArtwork, select_startup_artwork
from .artwork_layout import ArtworkLayout, ArtworkLayoutStore

#: "自动匹配" choice in the Subtract tab's idle selector (uses name matching).
AUTO_IDLE_CHOICE = "自动（按名称匹配，ads 优先）"

#: "自动匹配" choice in the BodyGroup tab's per-activity selectors.
AUTO_BG_CHOICE = "自动（按名称匹配）"

INPUT_MODES = ("单个文件", "文件夹", "文件夹（含子目录）")

#: ik_rule_handling values and their labels ("默认" is the parser's own output).

SUPPORTED_VERSIONS_TEXT = "44-49, 54-59"

def _config_dir() -> str:
    """Writable config location: next to the exe when frozen, repo root in dev."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_CONFIG_FILE = os.path.join(_config_dir(), "loka_config.json")

#: Fixed application icon and fallback avatar; startup artwork is selected below.
def _pkg_dir() -> str:
    """Directory of this module, resolving PyInstaller's one-file extraction
    layout (``sys._MEIPASS/loka/gui``) when frozen."""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "loka", "gui")
    return os.path.dirname(os.path.abspath(__file__))


_ASSET_DIR = os.path.join(_pkg_dir(), "assets")
_LOGO_IMAGE = os.path.join(_ASSET_DIR, "logo.png")
_ICON_FILE = os.path.join(_ASSET_DIR, "loka.ico")


def _default_artwork_layout_store() -> ArtworkLayoutStore:
    return ArtworkLayoutStore(Path(_ASSET_DIR) / 'startup' / 'layouts.json')

#: Width of the permanently expanded navigation panel.  The panel is never
#: collapsible, so this is also what the content area has to fit beside.
_NAV_EXPAND_WIDTH = 200


def classify_dropped_path(path: str) -> str:
    """Return ``"folder"``, ``"mdl"`` or ``"other"`` for a dropped path.

    Previously lived in ``loka.gui.dnd``, the Tk-era drag-and-drop module
    whose top-level ``import tkinter`` dragged the whole Tcl/Tk runtime into the
    frozen executable.  Qt delivers drops itself, so only this pure-``os``
    classifier was still needed.
    """
    if os.path.isdir(path):
        return "folder"
    if os.path.isfile(path) and path.lower().endswith(".mdl"):
        return "mdl"
    return "other"



class _BoolValidator(OptionsValidator):
    """Accept JSON booleans only and recover malformed values to the default."""

    def __init__(self, default: bool):
        super().__init__([default, not default])

    def validate(self, value):
        return type(value) is bool


class _PreviewDistanceValidator(ConfigValidator):
    def validate(self,value):
        return type(value) is int and 70<=value<=160

    def correct(self,value):
        return value if self.validate(value) else 90


class GuiConfig(QConfig):
    """Persisted GUI settings (theme comes from the base Config)."""

    game_path = ConfigItem("ToolPaths", "GamePath", "")
    studiomdl_path = ConfigItem("ToolPaths", "StudiomdlPath", "")
    #: Advanced mode reveals the three specialized pages and detail options.
    advanced = ConfigItem("General", "Advanced", False)
    conversion_isolate_materials = ConfigItem("WeaponConversion", "IsolateMaterials", False, _BoolValidator(False))
    conversion_normalize_vmt = ConfigItem("WeaponConversion", "NormalizeVmt", False, _BoolValidator(False))
    conversion_keep_ads = ConfigItem("WeaponConversion", "KeepAds", True, _BoolValidator(True))
    conversion_keep_temp = ConfigItem("WeaponConversion", "KeepTemp", False, _BoolValidator(False))
    conversion_l4n = ConfigItem("WeaponConversion", "L4n", False, _BoolValidator(False))
    conversion_melee_layer_fadein_fix = ConfigItem("WeaponConversion", "MeleeLayerFadeinFix", False, _BoolValidator(False))
    conversion_fps_match = ConfigItem("WeaponConversion", "FpsMatch", False, _BoolValidator(False))
    conversion_exclude_inspect_fps = ConfigItem("WeaponConversion", "ExcludeInspectFps", False, _BoolValidator(False))
    conversion_audio_logic = ConfigItem("WeaponConversion", "AudioLogic", "frequency",
                                       OptionsValidator(["frequency", "balanced", "complete"]))
    conversion_ik_rule_handling = ConfigItem("WeaponConversion", "IkRuleHandling", "delete",
                                            OptionsValidator(["delete","default","usesource"]))
    # Loading legacy "auto" values corrects them to the first explicit choice.
    conversion_source_pistol_mode = ConfigItem("WeaponConversion", "SourcePistolMode", "single",
                                               OptionsValidator(["single","dual"]))
    action_camera_mode = ConfigItem("ActionCorrection", "CameraMode", "fixed", OptionsValidator(["fixed","follow"]))
    action_preview_distance = ConfigItem("ActionCorrection", "PreviewDistance", 90, _PreviewDistanceValidator())
    action_compare_original = ConfigItem("ActionCorrection", "CompareOriginal", False, _BoolValidator(False))
    action_loop = ConfigItem("ActionCorrection", "LoopPlayback", False, _BoolValidator(False))
    action_play_audio = ConfigItem("ActionCorrection", "PlayAudio", False, _BoolValidator(False))
    action_details_open = ConfigItem("ActionCorrection", "DetailsOpen", False, _BoolValidator(False))


CFG = GuiConfig()

# migrate the pre-rename config files if they exist
def _ensure_config_file() -> str:
    if not os.path.isfile(_CONFIG_FILE):
        candidates = [
            os.path.join(os.path.dirname(_CONFIG_FILE), "gui_config.json"),
            os.path.join(os.path.dirname(_CONFIG_FILE), "config", "qconfig.json"),
        ]
        for old in candidates:
            if os.path.isfile(old):
                try:
                    with open(old, encoding="utf-8") as f:
                        data = f.read()
                    with open(_CONFIG_FILE, "w", encoding="utf-8", newline="\n") as f:
                        f.write(data)
                    break
                except OSError:
                    pass
    return _CONFIG_FILE


qconfig.load(_ensure_config_file(), CFG)


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------
class _WorkerSignals(QObject):
    """Thread-safe bridge for the decompile worker (queued across threads)."""

    log = Signal(str)
    done = Signal(str)
    weapon_inspection = Signal(object)
    weapon_scan_outcome = Signal(object)
    weapon_result = Signal(object)
    correction_loaded = Signal(object)
    correction_failed = Signal(str)
    correction_save_finished = Signal(object)
    correction_preview_cleared = Signal()


def _vbox(parent: QWidget, margin: int = 32) -> QVBoxLayout:
    layout = QVBoxLayout(parent)
    layout.setContentsMargins(margin, margin, margin, margin)
    layout.setSpacing(14)
    layout.setAlignment(Qt.AlignTop)
    return layout


class LogCard(CardWidget):
    """A titled card with a read-only monospace-ish log text box."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(8)
        layout.addWidget(SubtitleLabel(title, self))

        self.text = TextEdit(self)
        self.text.setReadOnly(True)
        self.text.setFixedHeight(220)
        layout.addWidget(self.text)

    def append(self, message: str) -> None:
        self.text.append(message)
        scrollbar = self.text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def render_home_avatar(source: QPixmap, diameter: int = 280,
                       zoom: float = 1.35, device_ratio: float = 1.0,
                       offset_x: float = 0.0, offset_y: float = 0.0) -> QPixmap:
    """Render an enlarged circular viewport without changing the source asset."""
    if source.isNull():
        return QPixmap()
    result = QPixmap(round(diameter * device_ratio), round(diameter * device_ratio))
    result.setDevicePixelRatio(device_ratio)
    result.fill(Qt.transparent)
    painter = QPainter(result)
    painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    circle = QRectF(1, 1, diameter - 2, diameter - 2)
    path = QPainterPath()
    path.addEllipse(circle)
    painter.setClipPath(path)
    scale = max(diameter / source.width(), diameter / source.height()) * zoom
    width, height = source.width() * scale, source.height() * scale
    # Center the face rather than the larger silhouette of hair around it.
    target = QRectF(diameter / 2 - width * 0.425 + offset_x,
                    diameter / 2 - height * 0.52 + offset_y, width, height)
    painter.drawPixmap(target, source, QRectF(source.rect()))
    painter.setClipping(False)
    painter.setPen(QPen(QColor(128, 128, 128, 100), 1.5))
    painter.drawEllipse(circle)
    painter.end()
    return result


class HomeInterface(ScrollArea):
    """Nav item 1: logo + author/copyright information."""

    artwork_layout_changed = Signal(object)
    artwork_layout_confirmed = Signal(object)
    artwork_preview_changed = Signal(str)

    def __init__(self, parent: FluentWindow | None = None,
                 artwork: StartupArtwork | None = None,
                 artwork_layout: ArtworkLayout | None = None,
                 show_tuning: bool = False) -> None:
        super().__init__(parent)
        self.startup_artwork = artwork
        self.artwork_layout = artwork_layout or ArtworkLayout()
        self.artwork_controls = {}
        self.scrollWidget = QWidget(self)
        layout = _vbox(self.scrollWidget)
        layout.addStretch(2)

        logo = self.logo_label = QLabel(self)
        pixmap = QPixmap(str(artwork.avatar_path) if artwork else _LOGO_IMAGE)
        if pixmap.isNull():
            pixmap = QPixmap(_LOGO_IMAGE)
        self._avatar_source = pixmap
        logo.setFixedSize(280, 280)
        self._render_avatar()
        layout.addWidget(logo, 0, Qt.AlignHCenter)
        layout.addSpacing(12)

        by_label = BodyLabel(
            'Loka by <a href="https://space.bilibili.com/13538760">Punizione</a>', self)
        by_label.setOpenExternalLinks(True)
        by_label.setAlignment(Qt.AlignHCenter)
        layout.addWidget(by_label)

        version_label = BodyLabel(f"Version: {__version__}", self)
        version_label.setAlignment(Qt.AlignHCenter)
        layout.addWidget(version_label)

        layout.addSpacing(10)
        for text in (
            "本工具开源，免费，如果你是花钱获取的，说明你被骗了",
            "作者无Q群不接定制没有爱发电，不教任何提取/加密/外挂相关知识",
        ):
            note = BodyLabel(text, self)
            note.setWordWrap(True)
            note.setAlignment(Qt.AlignHCenter)
            note.setStyleSheet("color: gray;")
            layout.addWidget(note)

        layout.addStretch(3)

        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setViewportMargins(0, 0, 0, 0)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setObjectName("homeInterface")
        self.enableTransparentBackground()
        if show_tuning:
            self._create_artwork_tuning()

    def _render_avatar(self) -> None:
        transform = self.artwork_layout.avatar
        self.logo_label.setPixmap(render_home_avatar(
            self._avatar_source, zoom=transform.scale,
            device_ratio=self.logo_label.devicePixelRatioF(),
            offset_x=transform.x, offset_y=transform.y))

    def _create_artwork_tuning(self) -> None:
        # An overlay at the left keeps the normal avatar/copyright layout fixed.
        panel = self.artwork_tuning_panel = CardWidget(self.scrollWidget)
        panel.setFixedWidth(230)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)
        self.artwork_group_combo = ComboBox(panel)
        self.artwork_group_combo.addItems([f'第 {i} 组' for i in range(1, 7)] + ['夜间背景'])
        self.artwork_group_combo.setCurrentIndex(6 if self.startup_artwork.is_night
                                                 else self.startup_artwork.pair_id - 1)
        self.artwork_group_combo.currentIndexChanged.connect(
            lambda index: self.artwork_preview_changed.emit(PREVIEW_CHOICES[index]))
        layout.addWidget(self.artwork_group_combo)
        hint = CaptionLabel('X 向右、Y 向下，单位为 px', panel)
        layout.addWidget(hint)
        for prefix, title, transform in (
                ('avatar', '头像（圆内图像）', self.artwork_layout.avatar),
                ('background', '背景', self.artwork_layout.background)):
            layout.addWidget(BodyLabel(title, panel))
            for field, label in (('x', 'X'), ('y', 'Y'), ('scale', '缩放')):
                row = QHBoxLayout()
                row.addWidget(CaptionLabel(label, panel))
                box = DoubleSpinBox(panel)
                box.setRange(0.1, 5.0) if field == 'scale' else box.setRange(-2000.0, 2000.0)
                box.setDecimals(2 if field == 'scale' else 1)
                box.setSingleStep(0.05 if field == 'scale' else 1.0)
                box.setValue(getattr(transform, field))
                row.addWidget(box, 1)
                layout.addLayout(row)
                self.artwork_controls[f'{prefix}_{field}'] = box
                box.valueChanged.connect(
                    lambda value, p=prefix, f=field: self._artwork_value_changed(p, f, value))
        self.artwork_confirm_button = PrimaryPushButton('确认当前组', panel)
        self.artwork_confirm_button.clicked.connect(
            lambda: self.artwork_layout_confirmed.emit(self.artwork_layout))
        layout.addWidget(self.artwork_confirm_button)
        self.artwork_tuning_status = CaptionLabel('切换保留草稿，确认后保存', panel)
        self.artwork_tuning_status.setWordWrap(True)
        layout.addWidget(self.artwork_tuning_status)
        panel.adjustSize()
        panel.move(16, 16)
        panel.raise_()

    def set_preview_artwork(self, artwork: StartupArtwork, layout: ArtworkLayout) -> None:
        self.startup_artwork = artwork
        self.artwork_layout = layout
        self._avatar_source = QPixmap(str(artwork.avatar_path))
        self._render_avatar()
        widgets = list(self.artwork_controls.values()) + [self.artwork_group_combo]
        blocked = [widget.blockSignals(True) for widget in widgets]
        try:
            for prefix in ('avatar', 'background'):
                transform = getattr(layout, prefix)
                for field in ('x', 'y', 'scale'):
                    self.artwork_controls[f'{prefix}_{field}'].setValue(getattr(transform, field))
            self.artwork_group_combo.setCurrentIndex(6 if artwork.is_night else artwork.pair_id - 1)
        finally:
            for widget, previous in zip(widgets, blocked):
                widget.blockSignals(previous)
        self.artwork_tuning_status.setText('切换保留草稿，确认后保存')

    def _artwork_value_changed(self, prefix: str, field: str, value: float) -> None:
        transform = replace(getattr(self.artwork_layout, prefix), **{field: value})
        self.artwork_layout = replace(self.artwork_layout, **{prefix: transform})
        self._render_avatar()
        self.artwork_tuning_status.setText('修改实时预览，确认后保存')
        self.artwork_layout_changed.emit(self.artwork_layout)

    def event(self, event: QEvent) -> bool:
        handled = super().event(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange and hasattr(self, '_avatar_source'):
            self._render_avatar()
        return handled



class SettingsInterface(ScrollArea):
    """Nav item 2: tool paths used by the one-click pipeline."""

    def __init__(self, parent: FluentWindow | None = None) -> None:
        super().__init__(parent)
        self.scrollWidget = QWidget(self)
        layout = _vbox(self.scrollWidget)
        layout.addWidget(SubtitleLabel("设置", self))

        group = SettingCardGroup("工具路径", self)

        self.game_card = PushSettingCard(
            "浏览",
            FluentIcon.GLOBE,
            "游戏路径",
            CFG.game_path.value or "未设置（用于自动查找 studiomdl.exe 与编译 -game 目录）",
            self,
        )
        self.game_card.clicked.connect(self._browse_game)

        self.compiler_card = PushSettingCard(
            "浏览",
            FluentIcon.APPLICATION,
            "studiomdl.exe / 第三方编译器路径",
            CFG.studiomdl_path.value or "未设置（请选择模型编译器）",
            self,
        )
        self.compiler_card.clicked.connect(self._browse_compiler)

        self.find_card = PushSettingCard(
            "自动查找",
            FluentIcon.SEARCH,
            "从游戏路径查找编译器",
            "在游戏路径的 bin 目录（或深度 3 内）查找 studiomdl.exe，并定位 -game 目录",
            self,
        )
        self.find_card.clicked.connect(self._auto_find)

        self.advanced_card = SwitchSettingCard(
            FluentIcon.DEVELOPER_TOOLS,
            "高级",
            configItem=CFG.advanced,
            parent=self,
        )

        group.addSettingCard(self.game_card)
        group.addSettingCard(self.compiler_card)
        group.addSettingCard(self.find_card)
        group.addSettingCard(self.advanced_card)
        layout.addWidget(group)

        hint = CaptionLabel(
            "填游戏路径时 兼容ADS处理会自动查找其中的 studiomdl.exe（bin 下）；"
            "也可直接指定兼容的模型编译器。设置会自动保存。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.update_link = BodyLabel(
            '<a href="https://github.com/Punizione/Loka/releases">[更新链接]</a> 随缘更新',
            self.scrollWidget,
        )
        self.update_link.setOpenExternalLinks(True)
        layout.addWidget(self.update_link)

        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setViewportMargins(0, 0, 0, 0)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setObjectName("settingsInterface")
        self.enableTransparentBackground()


    def _browse_game(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择游戏路径", CFG.game_path.value or "")
        if not path:
            return
        CFG.game_path.value = path
        qconfig.save()
        self.game_card.setContent(path)
        self._toast("游戏路径已设置", "success")

    def _browse_compiler(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择模型编译器", CFG.studiomdl_path.value or "",
            "Executable (*.exe);;All files (*.*)",
        )
        if not path:
            return
        CFG.studiomdl_path.value = path
        qconfig.save()
        self.compiler_card.setContent(path)
        self._toast("编译器路径已设置", "success")

    def _auto_find(self) -> None:
        game_path = CFG.game_path.value or ""
        if not game_path:
            self._toast("请先设置游戏路径", "warning")
            return
        exe_path, game_dir = find_studiomdl(game_path)
        if not exe_path:
            self._toast(f"在游戏路径中未找到 studiomdl.exe：{game_path}", "error")
            return
        CFG.studiomdl_path.value = exe_path
        qconfig.save()
        self.compiler_card.setContent(exe_path)
        message = f"找到编译器: {exe_path}"
        if game_dir:
            message += f"\n-game 目录: {game_dir}"
        self._toast(message, "success")

    def _toast(self, message: str, kind: str) -> None:
        show = getattr(InfoBar, kind)
        show(
            "设置", message, self, duration=3000, parent=self,
        )


class OneClickInterface(ScrollArea):
    """Advanced nav item 4: backup -> decompile -> compile pipeline."""

    def __init__(self, parent: FluentWindow | None = None) -> None:
        super().__init__(parent)
        self.scrollWidget = QWidget(self)
        layout = _vbox(self.scrollWidget)
        layout.addWidget(SubtitleLabel("兼容ADS处理", self))

        note = CaptionLabel(
            "备份原始模型 → 反编译（自动开启 subtract 修正 + BodyGroup处理）→ "
            "编译 → 产物回填原位。编译失败会自动还原备份。", self)
        note.setWordWrap(True)
        layout.addWidget(note)

        input_card = CardWidget(self)
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(18, 14, 18, 14)
        input_layout.addWidget(BodyLabel("输入模型（.mdl 文件或文件夹）", input_card))
        path_row = QHBoxLayout()
        self.input_edit = LineEdit(input_card)
        self.input_edit.setPlaceholderText("拖入 .mdl 文件或文件夹，或点浏览选择")
        path_row.addWidget(self.input_edit, 1)
        browse = PushButton("浏览", input_card)
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        input_layout.addLayout(path_row)
        layout.addWidget(input_card)

        flow_card = CardWidget(self)
        flow_layout = QVBoxLayout(flow_card)
        flow_layout.setContentsMargins(18, 14, 18, 14)
        flow_layout.addWidget(BodyLabel("执行流程", flow_card))
        steps = QLabel(
            "① 原始模型及附属文件备份到 <mdl目录>/source\n"
            "② 反编译到 <mdl目录>/dec（qc/smd 固定位置，忽略 Output folder）\n"
            "③ 用「设置」页配置的编译器编译 qc\n"
            "④ 编译产物移回原模型位置",
            flow_card,
        )
        steps.setWordWrap(True)
        flow_layout.addWidget(steps)
        layout.addWidget(flow_card)

        options_card = CardWidget(self)
        options_layout = QVBoxLayout(options_card)
        options_layout.setContentsMargins(18, 14, 18, 14)
        options_layout.addWidget(BodyLabel("处理选项", options_card))
        ik_row = QHBoxLayout()
        ik_row.addWidget(BodyLabel("IK 处理 (ikrule):", options_card))
        self.ik_box = ComboBox(options_card)
        self.ik_box.addItems(list(IK_HANDLING_LABELS.values()))
        self.ik_box.setMinimumWidth(140)
        ik_row.addWidget(self.ik_box, 1)
        options_layout.addLayout(ik_row)
        self.fadein_check = CheckBox("推击淡入", options_card)
        self.fadein_check.setChecked(False)
        options_layout.addWidget(self.fadein_check)
        fadein_note = CaptionLabel(
            "勾选后，普通、15连及 ADS/MIXED 推击动画的 fadein 强制为 0.2", options_card)
        fadein_note.setWordWrap(True)
        options_layout.addWidget(fadein_note)
        layout.addWidget(options_card)

        run_row = QHBoxLayout()
        self.run_button = PrimaryPushButton("开始处理", self)
        self.run_button.clicked.connect(self._run)
        run_row.addWidget(self.run_button)
        self.progress = IndeterminateProgressBar(self)
        self.progress.hide()
        run_row.addWidget(self.progress, 1)
        layout.addLayout(run_row)

        self.log_card = LogCard("运行日志", self)
        layout.addWidget(self.log_card)

        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setViewportMargins(0, 0, 0, 0)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setObjectName("oneClickInterface")
        self.enableTransparentBackground()


    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "选择输入模型", "", "MDL (*.mdl)")
        if path:
            self.input_edit.setText(path)

    def _run(self) -> None:
        window: MainWindow = self.window()
        window.run_one_click_page(self)


class DecompileInterface(ScrollArea):
    """Nav item 5: input selection, options, run button and log."""

    def __init__(self, parent: FluentWindow | None = None) -> None:
        super().__init__(parent)
        self.scrollWidget = QWidget(self)
        layout = _vbox(self.scrollWidget)
        layout.addWidget(SubtitleLabel("反编译", self))

        # Input card
        input_card = CardWidget(self)
        grid = QVBoxLayout(input_card)
        grid.setContentsMargins(18, 14, 18, 14)
        grid.addWidget(BodyLabel("输入 / 输出", input_card))

        mode_row = QHBoxLayout()
        mode_row.addWidget(BodyLabel("模式:", input_card))
        self.mode_box = ComboBox(input_card)
        self.mode_box.addItems(INPUT_MODES)
        mode_row.addWidget(self.mode_box)
        mode_row.addStretch(1)
        grid.addLayout(mode_row)

        grid.addWidget(CaptionLabel("输入 MDL / 文件夹:", input_card))
        input_row = QHBoxLayout()
        self.input_edit = LineEdit(input_card)
        input_row.addWidget(self.input_edit, 1)
        self.input_browse = PushButton("浏览", input_card)
        self.input_browse.clicked.connect(self._browse_input)
        input_row.addWidget(self.input_browse)
        grid.addLayout(input_row)

        grid.addWidget(CaptionLabel("输出文件夹（留空 = 输入目录下的 dec）:", input_card))
        output_row = QHBoxLayout()
        self.output_edit = LineEdit(input_card)
        output_row.addWidget(self.output_edit, 1)
        self.output_browse = PushButton("浏览", input_card)
        self.output_browse.clicked.connect(self._browse_output)
        output_row.addWidget(self.output_browse)
        grid.addLayout(output_row)

        self.drop_hint = CaptionLabel("支持将 .mdl 文件或文件夹直接拖入窗口任意位置", input_card)
        grid.addWidget(self.drop_hint)
        layout.addWidget(input_card)

        # Files to write
        files_card = CardWidget(self)
        files_layout = QVBoxLayout(files_card)
        files_layout.setContentsMargins(18, 14, 18, 14)
        files_layout.addWidget(BodyLabel("文件输出", files_card))
        files_grid = QHBoxLayout()
        self.file_checks: dict[str, CheckBox] = {}
        files = [
            ("write_qc_file", "QC 文件 (.qc)", True),
            ("write_reference_mesh_smd_file", "参考网格 SMD", True),
            ("write_lod_mesh_smd_files", "LOD 网格 SMD", True),
            ("write_physics_mesh_smd_file", "物理网格 SMD", True),
            ("write_vertex_animation_vta_file", "顶点动画 (.vta)", True),
            ("write_bone_animation_smd_files", "骨骼动画 SMD", True),
            ("write_procedural_bones_vrd_file", "程序骨骼 (.vrd)", True),
            ("write_declare_sequence_qci_file", "DeclareSequence QCI 文件", False),
            ("write_texture_files", "贴图 BMP 文件", False),
        ]
        columns = [QVBoxLayout() for _ in range(2)]
        for index, (attribute, label, default) in enumerate(files):
            box = CheckBox(label, files_card)
            box.setChecked(default)
            self.file_checks[attribute] = box
            columns[index % 2].addWidget(box)
        for column in columns:
            files_grid.addLayout(column)
        files_grid.addStretch(1)
        files_layout.addLayout(files_grid)
        layout.addWidget(files_card)

        # Text options
        text_card = CardWidget(self)
        text_layout = QHBoxLayout(text_card)
        text_layout.setContentsMargins(18, 14, 18, 14)
        text_layout.addWidget(BodyLabel("文本选项", text_card))
        text_column = QVBoxLayout()
        text_layout.addLayout(text_column, 2)
        self.text_checks: dict[str, CheckBox] = {}
        texts = [
            ("remove_path_from_smd_material_file_names", "移除 SMD 材质名中的路径", True),
            ("use_non_valve_uv_conversion", "使用非 Valve UV 转换", False),
            ("stricter_format", "更严格的格式（无头部注释）", False),
            ("prefix_file_names_with_model_name", "文件名前加模型名前缀", False),
            ("bone_animation_place_in_subfolder", "动画 SMD 放入子文件夹", True),
        ]
        for attribute, label, default in texts:
            box = CheckBox(label, text_card)
            box.setChecked(default)
            self.text_checks[attribute] = box
            text_column.addWidget(box)

        combo_column = QVBoxLayout()
        text_layout.addLayout(combo_column)
        text_layout.addStretch(1)
        combo_column.addWidget(BodyLabel("覆盖 MDL 版本:", text_card))
        self.version_box = ComboBox(text_card)
        self.version_box.addItems(["不覆盖"] + SUPPORTED_VERSIONS_TEXT.replace(" ", "").split(","))
        combo_column.addWidget(self.version_box)
        layout.addWidget(text_card)

        # Run row
        run_row = QHBoxLayout()
        self.run_button = PrimaryPushButton("开始反编译", self)
        self.run_button.clicked.connect(self._run)
        run_row.addWidget(self.run_button)
        self.cancel_button = PushButton("取消", self)
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.setEnabled(False)
        run_row.addWidget(self.cancel_button)
        self.progress = IndeterminateProgressBar(self)
        self.progress.hide()
        run_row.addWidget(self.progress, 1)
        layout.addLayout(run_row)

        self.log_card = LogCard("运行日志", self)
        layout.addWidget(self.log_card)

        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setViewportMargins(0, 0, 0, 0)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setObjectName("decompileInterface")
        self.enableTransparentBackground()

        self._cancel_event = threading.Event()

    # -- input helpers -------------------------------------------------
    def _browse_input(self) -> None:
        if self.mode_box.currentIndex() == 0:  # File
            path, _filter = QFileDialog.getOpenFileName(self, "选择输入模型", "", "MDL (*.mdl)")
        else:
            path = QFileDialog.getExistingDirectory(self, "选择输入文件夹")
        if path:
            self.set_input_path(path)

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择输出文件夹")
        if path:
            self.output_edit.setText(path)

    def set_input_path(self, path: str) -> None:
        """Set the input and re-default the output to ``<input dir>/dec``."""
        self.input_edit.setText(path)
        self.output_edit.setText(os.path.join(os.path.dirname(os.path.abspath(path)), "dec"))

    def _run(self) -> None:
        window: MainWindow = self.window()
        window.run_decompile_page(self)

    def _cancel(self) -> None:
        self._cancel_event.set()
        self.log_card.append("已请求取消，正在完成当前模型 ...")

    def collect_options(self) -> DecompileOptions:
        options = DecompileOptions()
        options.game_path = (CFG.game_path.value or "").strip()
        for attribute, box in self.file_checks.items():
            setattr(options, attribute, box.isChecked())
        for attribute, box in self.text_checks.items():
            setattr(options, attribute, box.isChecked())
        if self.version_box.currentText() != "不覆盖":
            options.override_mdl_version = int(self.version_box.currentText())
        # IK 处理 moved to the one-click page.
        main_window: MainWindow = self.window()
        for value, label in IK_HANDLING_LABELS.items():
            if label == main_window.one_click_page.ik_box.currentText():
                options.ik_rule_handling = value
                break
        # Subtract修正 / BodyGroup处理 settings come from their pages and
        # always apply (their switches were removed - the one-click flow is
        # the primary path and always uses them).
        options.subtract_correction = True
        choice = main_window.subtract_page.idle_choice()
        if choice != AUTO_IDLE_CHOICE:
            options.subtract_correction_idle = choice
        if main_window.subtract_page.axis_offsets:
            options.subtract_axis_offsets = dict(main_window.subtract_page.axis_offsets)
        if main_window.subtract_page.item_bases:
            options.subtract_bases = dict(main_window.subtract_page.item_bases)
        options.bodygroup_events = True
        event_map = main_window.bodygroup_page.event_map()
        if event_map:
            options.bodygroup_event_map = event_map
        return options


class SubtractInterface(ScrollArea):
    """Advanced nav item 6: the subtract-correction tuning table."""

    def __init__(self, parent: FluentWindow | None = None) -> None:
        super().__init__(parent)
        self.scrollWidget = QWidget(self)
        layout = _vbox(self.scrollWidget)
        layout.addWidget(SubtitleLabel("Subtract修正", self))

        note = CaptionLabel(
            "用真实 idle 作为 delta 序列的 subtract 基准，替代空的 *_corrective_animation；"
            "设置在反编译时自动应用。", self)
        note.setWordWrap(True)
        layout.addWidget(note)

        self.path_label = CaptionLabel("当前输入: 未选择", self)
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)

        toolbar = QHBoxLayout()
        self.idle_box = ComboBox(self)
        self.idle_box.addItem(AUTO_IDLE_CHOICE)
        self.idle_box.setMinimumWidth(96)
        self.idle_box.currentIndexChanged.connect(self._sync_idle_label)
        toolbar.addWidget(BodyLabel("subtract 基准 (idle):", self))
        toolbar.addWidget(self.idle_box, 1)
        self.load_button = PushButton("从输入 MDL 读取", self)
        self.load_button.clicked.connect(self._load_overview)
        toolbar.addWidget(self.load_button)
        layout.addLayout(toolbar)

        self.table = TableWidget(self)
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["名称", "类型", "标记", "生成subtract", "帧数", "subtract基准", "轴向调整(°)"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(280)
        self._make_table_adaptive()
        self.table.itemSelectionChanged.connect(self._on_select)
        layout.addWidget(self.table, 1)

        edit_card = CardWidget(self)
        edit_layout = QHBoxLayout(edit_card)
        edit_layout.setContentsMargins(18, 14, 18, 14)
        edit_column = QVBoxLayout()
        edit_layout.addLayout(edit_column, 1)
        edit_column.addWidget(BodyLabel("选中动作的轴向调整 (°) 与 subtract 基准", edit_card))
        editor_row = QHBoxLayout()
        self.axis_spins: list[DoubleSpinBox] = []
        for label in ("X", "Y", "Z"):
            editor_row.addWidget(BodyLabel(label, edit_card))
            spin = DoubleSpinBox(edit_card)
            spin.setRange(-720.0, 720.0)
            spin.setDecimals(2)
            spin.setMinimumWidth(64)
            editor_row.addWidget(spin, 1)
            self.axis_spins.append(spin)
        edit_column.addLayout(editor_row)

        base_row = QHBoxLayout()
        self.base_box = ComboBox(edit_card)
        self.base_box.addItem(AUTO_IDLE_CHOICE)
        self.base_box.setMinimumWidth(96)
        base_row.addWidget(BodyLabel("基准:", edit_card))
        base_row.addWidget(self.base_box, 1)
        apply_button = PushButton("应用到选中动作", edit_card)
        apply_button.clicked.connect(self._apply_to_selected)
        base_row.addWidget(apply_button)
        clear_button = PushButton("清除选中动作", edit_card)
        clear_button.clicked.connect(self._clear_selected)
        base_row.addWidget(clear_button)
        edit_column.addLayout(base_row)
        layout.addWidget(edit_card)

        self._overview: MdlOverview | None = None
        self._row_items: list = []
        self.axis_offsets: dict[str, tuple[float, float, float]] = {}
        self.item_bases: dict[str, str] = {}

        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setViewportMargins(0, 0, 0, 0)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setObjectName("subtractInterface")
        self.enableTransparentBackground()


    # -- API used by the main window ----------------------------------
    def _make_table_adaptive(self) -> None:
        """Fit all columns into the viewport by stretching every column, so
        nothing gets clipped at the right edge at default window size."""
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setMinimumSectionSize(48)

    def idle_choice(self) -> str:
        return self.idle_box.currentText()

    def event_map(self) -> dict:  # pragma: no cover - symmetry with bodygroup page
        return {}

    # -- loading --------------------------------------------------------
    def _load_overview(self) -> None:
        window: MainWindow = self.window()
        input_path = window.decompile_page.input_edit.text().strip()
        if not input_path or not os.path.isfile(input_path):
            InfoBar.warning(
                "Subtract修正", "先在「兼容ADS处理」或「反编译」页选择一个 .mdl 文件，再点“从输入 MDL 读取”。",
                duration=3000, parent=self, position=InfoBarPosition.TOP,
            )
            return

        overview = read_mdl_overview(input_path)
        if overview.errors:
            InfoBar.error("Subtract修正", "\n".join(overview.errors), duration=-1,
                          parent=self, position=InfoBarPosition.TOP)
            return

        self._overview = overview
        self.table.clearContents()
        self.table.setRowCount(0)
        self._row_items = []
        for item in overview.items:
            if item.name.startswith("@"):
                continue
            flags = "+".join(
                name for name, present in (("loop", item.is_loop), ("delta", item.is_delta))
                if present
            ) or "-"
            base_text = self.item_bases.get(item.name, "-")
            offset = self.axis_offsets.get(item.name)
            offset_text = "/".join(f"{v:g}" for v in offset) if offset else "-"
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                item.name, item.kind_label, flags,
                "是" if item.is_subtract else "-", str(item.frame_count),
                base_text, offset_text,
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
            self._row_items.append(item)

        candidates = [item.name for item in overview.idle_candidates]
        self.idle_box.clear()
        self.idle_box.addItem(AUTO_IDLE_CHOICE)
        for name in candidates:
            self.idle_box.addItem(name)
        self.idle_box.setCurrentIndex(0)
        self.base_box.clear()
        self.base_box.addItem(AUTO_IDLE_CHOICE)
        for name in candidates:
            self.base_box.addItem(name)
        if candidates:
            self.log_append(f"可选 idle 基准: {', '.join(candidates)}")
        else:
            self.log_append("该模型没有找到符合条件（无 subtract、带 loop、名称含 idle）的 $animation。")

    def log_append(self, message: str) -> None:
        window: MainWindow = self.window()
        window.decompile_page.log_card.append(message)

    # -- selection editing ----------------------------------------------
    def _selected_item(self):
        rows = {index.row() for index in self.table.selectedIndexes()}
        if len(rows) != 1:
            return None
        return self._row_items[rows.pop()]

    def _on_select(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        offset = self.axis_offsets.get(item.name, (0.0, 0.0, 0.0))
        for spin, value in zip(self.axis_spins, offset):
            spin.setValue(float(value))
        base = self.item_bases.get(item.name, AUTO_IDLE_CHOICE)
        index = self.base_box.findText(base)
        self.base_box.setCurrentIndex(index if index >= 0 else 0)

    def _apply_to_selected(self) -> None:
        item = self._selected_item()
        if item is None:
            self.log_append("Subtract修正: 请先在表格中选中一个动作。")
            return
        offsets = tuple(spin.value() for spin in self.axis_spins)
        if any(abs(value) > 1e-9 for value in offsets):
            self.axis_offsets[item.name] = offsets
        else:
            self.axis_offsets.pop(item.name, None)
        base = self.base_box.currentText()
        if base != AUTO_IDLE_CHOICE:
            self.item_bases[item.name] = base
        else:
            self.item_bases.pop(item.name, None)
        self._refresh_row(item)

    def _clear_selected(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        self.axis_offsets.pop(item.name, None)
        self.item_bases.pop(item.name, None)
        self._refresh_row(item)

    def _refresh_row(self, item) -> None:
        for row, row_item in enumerate(self._row_items):
            if row_item.name != item.name:
                continue
            offset = self.axis_offsets.get(item.name)
            offset_text = "/".join(f"{v:g}" for v in offset) if offset else "-"
            base_text = self.item_bases.get(item.name, "-")
            self.table.setItem(row, 5, QTableWidgetItem(base_text))
            self.table.setItem(row, 6, QTableWidgetItem(offset_text))
            break

    def _sync_idle_label(self) -> None:
        pass


class BodyGroupInterface(ScrollArea):
    """Advanced nav item 7: AE_CL_BODYGROUP_SET_VALUE event mapping."""

    def __init__(self, parent: FluentWindow | None = None) -> None:
        super().__init__(parent)
        self.scrollWidget = QWidget(self)
        layout = _vbox(self.scrollWidget)
        layout.addWidget(SubtitleLabel("BodyGroup处理", self))

        note = CaptionLabel(
            "给 MIXED_ON/MIXED_OFF 及 ads 过渡序列添加 AE_CL_BODYGROUP_SET_VALUE 事件；"
            "设置在反编译时自动应用。", self)
        note.setWordWrap(True)
        layout.addWidget(note)

        self.path_label = CaptionLabel("当前输入: 未选择", self)
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)

        toolbar = QHBoxLayout()
        self.load_button = PushButton("从输入 MDL 读取", self)
        self.load_button.clicked.connect(self._load_overview)
        toolbar.addWidget(self.load_button)
        toolbar.addWidget(BodyLabel("混合 bodygroup:", self))
        self.group_box = ComboBox(self)
        self.group_box.setMinimumWidth(96)
        self.group_box.currentIndexChanged.connect(lambda _i: self._refresh())
        toolbar.addWidget(self.group_box, 1)
        layout.addLayout(toolbar)

        self.table = TableWidget(self)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["SMD 名称", "序号 (smd_index)", "自动匹配的活动"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(200)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setMinimumSectionSize(48)
        layout.addWidget(self.table)

        mapping_card = CardWidget(self)
        mapping_layout = QVBoxLayout(mapping_card)
        mapping_layout.setContentsMargins(18, 14, 18, 14)
        mapping_layout.addWidget(BodyLabel("活动 → bodygroup 条目 映射", mapping_card))
        self.activity_boxes: dict[str, ComboBox] = {}
        self.activity_infos: dict[str, CaptionLabel] = {}
        for activity in MIXED_ACTIVITIES:
            row = QHBoxLayout()
            row.addWidget(BodyLabel(activity + ":", mapping_card))
            box = ComboBox(mapping_card)
            self.activity_boxes[activity] = box
            row.addWidget(box, 1)
            info = CaptionLabel("-", mapping_card)
            self.activity_infos[activity] = info
            row.addWidget(info, 2)
            mapping_layout.addLayout(row)
        layout.addWidget(mapping_card)

        self._overview: MdlOverview | None = None
        self._mixed_bodygroups = []

        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setViewportMargins(0, 0, 0, 0)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setObjectName("bodygroupInterface")
        self.enableTransparentBackground()


    # -- API used by the main window ------------------------------------
    def event_map(self) -> dict[str, str]:
        return {
            activity: box.currentText()
            for activity, box in self.activity_boxes.items()
            if box.currentText() != AUTO_BG_CHOICE
        }

    # -- loading ----------------------------------------------------------
    def _load_overview(self) -> None:
        window: MainWindow = self.window()
        input_path = window.decompile_page.input_edit.text().strip()
        if not input_path or not os.path.isfile(input_path):
            InfoBar.warning(
                "BodyGroup处理", "先在「兼容ADS处理」或「反编译」页选择一个 .mdl 文件，再点“从输入 MDL 读取”。",
                duration=3000, parent=self, position=InfoBarPosition.TOP,
            )
            return

        overview = read_mdl_overview(input_path)
        if overview.errors:
            InfoBar.error("BodyGroup处理", "\n".join(overview.errors), duration=-1, parent=self)
            return

        self._overview = overview
        self._mixed_bodygroups = overview.mixed_bodygroups
        if len(overview.bodygroup_names) < 3:
            self._log("该模型的 bodygroup 数量少于 3 个，不满足条件。")
        if not self._mixed_bodygroups:
            self._log("没有找到包含 mixed_ 开头 SMD 的 bodygroup。")

        self.group_box.blockSignals(True)
        self.group_box.clear()
        for group in self._mixed_bodygroups:
            self.group_box.addItem(group.name, userData=group)
        self.group_box.blockSignals(False)
        self._refresh()

    def _log(self, message: str) -> None:
        window: MainWindow = self.window()
        window.decompile_page.log_card.append("BodyGroup处理: " + message)

    def _selected_group(self):
        if not self._mixed_bodygroups:
            return None
        index = self.group_box.currentIndex()
        if index >= 0:
            return self._mixed_bodygroups[index]
        return self._mixed_bodygroups[0]

    def _refresh(self) -> None:
        group = self._selected_group()
        self.table.clearContents()
        self.table.setRowCount(0)
        entry_names: list[str] = []
        auto_by_index: dict[int, str] = {}

        if group is not None:
            for activity in MIXED_ACTIVITIES:
                default_name = DEFAULT_MIXED_SMD_NAMES.get(activity, "")
                for name, index in group.entries:
                    if name == default_name:
                        auto_by_index.setdefault(index, activity)
            entry_names = [name for name, _index in group.entries]
            for name, index in group.entries:
                row = self.table.rowCount()
                self.table.insertRow(row)
                for column, value in enumerate(
                    (name or "(blank)", str(index), auto_by_index.get(index, "-"))
                ):
                    self.table.setItem(row, column, QTableWidgetItem(value))
            if group.mixed_count < 4:
                self._log(
                    f"bodygroup \"{group.name}\" 只有 {group.mixed_count} 个 mixed_ "
                    "开头条目（一般最少 4 个）。"
                )

        sequences_by_activity: dict[str, list] = {}
        if self._overview is not None:
            for item in self._overview.items:
                if item.kind == "sequence" and item.activity in MIXED_ACTIVITIES:
                    sequences_by_activity.setdefault(item.activity, []).append(item)

        for activity in MIXED_ACTIVITIES:
            box = self.activity_boxes[activity]
            box.blockSignals(True)
            box.clear()
            box.addItem(AUTO_BG_CHOICE)
            for name in entry_names:
                box.addItem(name)
            box.setCurrentIndex(0)
            box.blockSignals(False)
            matched = sequences_by_activity.get(activity, [])
            if matched:
                names = ", ".join(item.name for item in matched)
                info_text = f"序列: {names} | 帧数: {matched[0].frame_count} | 事件帧: 0"
            elif self._overview is not None:
                info_text = "未找到对应 $sequence"
            else:
                info_text = "-"
            self.activity_infos[activity].setText(info_text)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(FluentWindow):
    def event(self, event):
        result = super().event(event)
        # Adding the first QOpenGLWidget recreates the top-level HWND. Qt's
        # replacement loses the Win32 frame styles qframeless initialized.
        # Defer until recreation completes, and coalesce its destroy/create pair.
        if (sys.platform == 'win32' and event.type() == QEvent.WinIdChange
                and hasattr(self, 'windowEffect')
                and not getattr(self, '_frameless_refresh_pending', False)):
            self._frameless_refresh_pending = True
            QTimer.singleShot(0, self, self._restore_native_frame)
        return result

    def _restore_native_frame(self):
        self._frameless_refresh_pending = False
        # A destroyed/closed window must not be recreated just to refresh styles.
        hwnd = int(self.internalWinId())
        if not hwnd:
            return
        # The OpenGL HWND can retain Qt's default application caption even
        # though QWidget still caches the versioned title. Synchronize Win32 too.
        import win32gui
        win32gui.SetWindowText(hwnd,f"Loka {__version__}")
        self.windowEffect.addWindowAnimation(hwnd)
        self.windowEffect.addShadowEffect(hwnd)
        if self.isMicaEffectEnabled():
            self.windowEffect.setMicaEffect(hwnd, isDarkTheme())

    def __init__(self, preview_artwork: str | None = None, *,
                 layout_store: ArtworkLayoutStore | None = None) -> None:
        super().__init__()
        if getattr(sys, 'frozen', False):
            preview_artwork = None
        self._artwork_preview_enabled = preview_artwork is not None
        self._artwork_avatar_drafts = {}
        self._artwork_background_drafts = {}
        self.startup_artwork = select_startup_artwork(preview=preview_artwork)
        if preview_artwork is not None:
            for path in (self.startup_artwork.avatar_path, self.startup_artwork.background_path):
                if not path.is_file():
                    raise FileNotFoundError(f"预览素材未就绪：{path}；请补齐该组素材后再验收。")
        self._artwork_layout_store = layout_store if layout_store is not None else _default_artwork_layout_store()
        self.artwork_layout = self._artwork_layout_store.load(self.startup_artwork)
        self.setWindowTitle(f"Loka {__version__}")
        self.setWindowIcon(QIcon(_ICON_FILE))
        # Clean title bar: drop the return arrow, but keep the app icon.
        # The 48px gap keeps nav items below the title bar row.
        self.navigationInterface.setReturnButtonVisible(False)
        self.navigationInterface.panel.topLayout.insertSpacing(0, 48)
        self.resize(1150, 780)
        self.setMinimumSize(980, 660)
        self.setAcceptDrops(True)

        self.home_page = HomeInterface(self, artwork=self.startup_artwork,
                                      artwork_layout=self.artwork_layout,
                                      show_tuning=preview_artwork is not None)
        self.home_page.artwork_layout_changed.connect(self._apply_artwork_layout)
        self.home_page.artwork_layout_confirmed.connect(self._confirm_artwork_layout)
        self.home_page.artwork_preview_changed.connect(self._switch_artwork_preview)
        self.settings_page = SettingsInterface(self)
        self.one_click_page = OneClickInterface(self)
        self.decompile_page = DecompileInterface(self)
        self.subtract_page = SubtractInterface(self)
        self.bodygroup_page = BodyGroupInterface(self)
        self.weapon_conversion_page = WeaponConversionInterface(lambda: CFG.get(CFG.advanced),self,config=CFG)
        self.action_correction_page = None

        self._init_navigation()
        self._init_theme()
        self._sync_inputs()

        self._worker: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._signals = _WorkerSignals()
        self._signals.log.connect(self._on_worker_log)
        self._signals.done.connect(self._on_worker_done)
        self._signals.weapon_inspection.connect(self.weapon_conversion_page.set_package)
        self._signals.weapon_scan_outcome.connect(self.weapon_conversion_page.on_scan_outcome)
        self._signals.weapon_result.connect(self.weapon_conversion_page.set_result)
        self._signals.correction_loaded.connect(self._on_correction_loaded)
        self._signals.correction_failed.connect(self._on_correction_failed)
        self._signals.correction_save_finished.connect(self._on_correction_save_finished)
        self._signals.correction_preview_cleared.connect(self._on_correction_preview_cleared)
        self._active_run_page = None
        self._active_scan_request = None

        self._init_background_image()
        self._prepare_preview_surface()

    def _prepare_preview_surface(self):
        # Adding the first OpenGL child to an already visible window makes Qt
        # recreate its native window. Establish that surface before first show;
        # the action page, models, textures and game archives remain lazy.
        from PySide6.QtOpenGLWidgets import QOpenGLWidget
        from .action_viewport import preview_surface_format
        self._preview_surface=QOpenGLWidget(self)
        self._preview_surface.setObjectName('previewSurfaceAnchor')
        self._preview_surface.setFormat(preview_surface_format())
        self._preview_surface.setFocusPolicy(Qt.NoFocus)
        self._preview_surface.setAttribute(Qt.WA_TransparentForMouseEvents,True)
        self._preview_surface.resize(1,1);self._preview_surface.hide()
        from .action_backdrop import PreviewBackdrop
        self._preview_backdrop=PreviewBackdrop(self,self._bg_label)
        qconfig.themeChangedFinished.connect(self._preview_backdrop.invalidate)

    def _apply_artwork_layout(self, layout: ArtworkLayout) -> None:
        self.artwork_layout = layout
        self._artwork_avatar_drafts[self.startup_artwork.avatar_path.name] = layout.avatar
        self._artwork_background_drafts[self.startup_artwork.background_path.name] = layout.background
        if hasattr(self, '_bg_label'):
            self._update_bg_image()

    def _switch_artwork_preview(self, preview: str) -> None:
        if not self._artwork_preview_enabled:
            return
        artwork = select_startup_artwork(preview=preview)
        if any(not path.is_file() for path in (artwork.avatar_path, artwork.background_path)):
            previous = 6 if self.startup_artwork.is_night else self.startup_artwork.pair_id - 1
            combo = self.home_page.artwork_group_combo
            blocked = combo.blockSignals(True)
            combo.setCurrentIndex(previous)
            combo.blockSignals(blocked)
            InfoBar.error('预览素材未就绪', '请补齐该组素材后再预览', duration=5000,
                          position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        self._apply_artwork_layout(self.artwork_layout)
        layout = self._artwork_layout_store.load(artwork)
        layout = replace(layout,
                         avatar=self._artwork_avatar_drafts.get(artwork.avatar_path.name, layout.avatar),
                         background=self._artwork_background_drafts.get(artwork.background_path.name, layout.background))
        self.startup_artwork = artwork
        self.artwork_layout = layout
        self.home_page.set_preview_artwork(artwork, layout)
        self._bg_pixmap = QPixmap(str(artwork.background_path))
        self._bg_fallback_used = False
        self._bg_label.graphicsEffect().setOpacity(artwork.background_opacity)
        self._bg_scaled = None
        self._update_bg_image()

    def _confirm_artwork_layout(self, layout: ArtworkLayout) -> None:
        if not self._artwork_preview_enabled:
            return
        try:
            path = self._artwork_layout_store.save(self.startup_artwork, layout)
        except (OSError, ValueError, TypeError) as error:
            self.home_page.artwork_tuning_status.setText('保存失败，请重试')
            InfoBar.error('布局保存失败', str(error), duration=5000,
                          position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        self.home_page.artwork_tuning_status.setText('当前组参数已保存')
        print(json.dumps({'artwork_layout_confirmed': 'night' if self.startup_artwork.is_night
                          else str(self.startup_artwork.pair_id),
                          'values': asdict(layout), 'path': str(path)}, ensure_ascii=False), flush=True)
        InfoBar.success('布局已保存', '当前组的头像与背景参数已确认', duration=3000,
                        position=InfoBarPosition.TOP_RIGHT, parent=self)

    # -- background watermark ---------------------------------------------
    def _init_background_image(self) -> None:
        """A 50%-opacity cutout pinned to the bottom-right, behind all pages
        (every page uses enableTransparentBackground, so it shows through)."""
        self._bg_label = QLabel(self)
        self._bg_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._bg_label.setStyleSheet("background: transparent")
        self._bg_pixmap = QPixmap(str(self.startup_artwork.background_path))
        self._bg_fallback_used = self._bg_pixmap.isNull()
        if self._bg_fallback_used:
            warnings.warn(f"启动背景素材未就绪：{self.startup_artwork.background_path}；暂用第 2 组背景。",
                          RuntimeWarning, stacklevel=2)
            self._bg_pixmap = QPixmap(os.path.join(_ASSET_DIR, 'startup', 'background_02.png'))
        opacity = QGraphicsOpacityEffect(self._bg_label)
        opacity.setOpacity(1.0 if self._bg_fallback_used else self.startup_artwork.background_opacity)
        self._bg_label.setGraphicsEffect(opacity)
        self._bg_scaled: QPixmap | None = None
        self._bg_scaled_size = (0, 0)
        self._bg_label.lower()
        self.background_pid_label = BodyLabel(self)
        self.background_pid_label.setObjectName('backgroundPidLabel')
        self.background_pid_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.background_pid_label.setStyleSheet('color: gray; background: transparent;')
        self.stackedWidget.currentChanged.connect(self._update_background_pid)
        self._update_bg_image()

    def _update_background_pid(self) -> None:
        label = self.background_pid_label
        if (self.stackedWidget.currentWidget() is not self.home_page
                or self._bg_pixmap.isNull()
                or (self.startup_artwork.is_night and not self._bg_fallback_used)):
            label.clear()
            label.hide()
            return
        pid = BACKGROUND_PIDS[1] if self._bg_fallback_used else self.startup_artwork.background_pid
        label.setText('pid:{' + pid + '}')
        label.adjustSize()
        label.move(self.width() - label.width() - 12, self.height() - label.height() - 12)
        label.show()
        label.raise_()

    def _update_bg_image(self) -> None:
        if hasattr(self,'_preview_backdrop'):self._preview_backdrop.invalidate()
        self._update_background_pid()
        if self._bg_pixmap.isNull():
            return
        target_h = int(self.height() * 0.55)
        if target_h < 40:
            target_h = 40
        target_w = max(1, self.width() - _NAV_EXPAND_WIDTH)
        transform = self.artwork_layout.background
        target_size = (target_w, target_h, transform.scale)
        if self._bg_scaled_size != target_size or self._bg_scaled is None:
            fitted = self._bg_pixmap.size().scaled(target_w, target_h, Qt.KeepAspectRatio)
            self._bg_scaled = self._bg_pixmap.scaledToHeight(
                max(1, round(fitted.height() * transform.scale)), Qt.SmoothTransformation)
            self._bg_scaled_size = target_size
        pm = self._bg_scaled
        full = QRect(round(self.width() - pm.width() + transform.x),
                     round(self.height() - pm.height() + transform.y), pm.width(), pm.height())
        visible = full.intersected(QRect(_NAV_EXPAND_WIDTH, 0, target_w, self.height()))
        if visible.isEmpty():
            self._bg_label.hide()
            return
        self._bg_label.setGeometry(visible)
        self._bg_label.setPixmap(pm.copy(visible.x() - full.x(), visible.y() - full.y(),
                                       visible.width(), visible.height()))
        self._bg_label.show()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_bg_label"):
            self._update_bg_image()

    # -- navigation -------------------------------------------------------
    def _init_navigation(self) -> None:
        self.addSubInterface(self.home_page, FluentIcon.HOME, "首页")
        self.addSubInterface(self.settings_page, FluentIcon.SETTING, "设置")
        self.addSubInterface(self.weapon_conversion_page, FluentIcon.SYNC, "武器转模")
        self.navigationInterface.addItem(routeKey='actionCorrectionInterface',icon=FluentIcon.PLAY,
                                         text='动作矫正',onClick=self.show_action_correction)
        self.addSubInterface(self.decompile_page, FluentIcon.DOCUMENT, "反编译")
        self._advanced_nav = (
            (self.one_click_page, FluentIcon.PLAY, "兼容ADS处理", self.weapon_conversion_page),
            (self.subtract_page, FluentIcon.SYNC, "Subtract修正", self.decompile_page),
            (self.bodygroup_page, FluentIcon.TILES, "BodyGroup处理", self.subtract_page),
        )

        # Always-on navigation chrome.  This used to live at the end of
        # _apply_advanced_mode, which only runs when the Advanced switch is
        # toggled -- so on a fresh start the panel stayed collapsible (48px,
        # icons only), the hamburger button was visible, and the theme entry
        # was missing entirely.  Set the width *before* disabling the
        # collapsible flag, because setCollapsible(False) expands immediately.
        self.navigationInterface.setExpandWidth(_NAV_EXPAND_WIDTH)
        self.navigationInterface.setCollapsible(False)
        self.navigationInterface.setMenuButtonVisible(False)
        self.navigationInterface.addItem(
            routeKey="theme",
            icon=FluentIcon.CONSTRACT,
            text="深浅色",
            onClick=self._toggle_theme,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )

        self._advanced_nav_visible = False
        if CFG.get(CFG.advanced):
            self._add_advanced_nav()
        else:
            # Keep each page's input/options and worker state. Pages that have
            # not entered the stack must be hidden to avoid floating over it.
            for page, _icon, _text, _after in self._advanced_nav:
                page.hide()

        self.settings_page.advanced_card.checkedChanged.connect(self._apply_advanced_mode)

    def _purge_nav_route(self, route_key: str) -> None:
        """Remove every nav widget carrying ``route_key``.

        ``NavigationInterface.insertItem`` does not de-duplicate the way
        ``addItem`` does, and ``removeWidget`` only pops the single entry
        recorded in ``panel.items``.  Inserting the same key twice therefore
        overwrites the dict entry while appending a second widget to the layout,
        and removing it deletes only the newer one -- the older widget stays
        orphaned and visible.  The symptom is a duplicated nav item for every
        time Advanced mode is switched back on, so sweep the layout as well.
        """
        panel = self.navigationInterface.panel
        while route_key in panel.items:
            self.navigationInterface.removeWidget(route_key)

        layout = panel.topLayout
        for index in reversed(range(layout.count())):
            item = layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is not None and widget.property("routeKey") == route_key:
                layout.takeAt(index)
                widget.setParent(None)
                widget.deleteLater()

    def _add_advanced_nav(self) -> None:
        """Insert specialized pages at their fixed positions without resetting state."""
        panel = self.navigationInterface.panel
        for page, icon, text, after in self._advanced_nav:
            self._purge_nav_route(page.objectName())
            route=after if isinstance(after,str) else after.objectName()
            if page is self.one_click_page:route='actionCorrectionInterface'
            index = panel.topLayout.indexOf(panel.widget(route)) + 1
            if self.stackedWidget.indexOf(page) == -1:
                self.stackedWidget.addWidget(page)
            self.navigationInterface.insertItem(
                index, routeKey=page.objectName(), icon=icon, text=text,
                onClick=lambda _clicked=False, p=page: self.switchTo(p),
            )
        # NOTE: do not call show() here.  QStackedWidget::addWidget already leaves
        # a non-current page hidden; showing it explicitly paints it on top of the
        # page that is actually current.
        self._advanced_nav_visible = True

    def _apply_advanced_mode(self, checked: bool) -> None:
        self.weapon_conversion_page.log_card.refresh()
        self.weapon_conversion_page.set_advanced(checked)
        if checked and not self._advanced_nav_visible:
            self._add_advanced_nav()
        elif not checked and self._advanced_nav_visible:
            if any(self.stackedWidget.currentWidget() is page for page, _icon, _text, _after in self._advanced_nav):
                self.switchTo(self.decompile_page)
            for page, _icon, _text, _after in self._advanced_nav:
                self._purge_nav_route(page.objectName())
                if self.stackedWidget.indexOf(page) != -1:
                    self.stackedWidget.removeWidget(page)
                # Removing a page leaves it parented to the window.
                page.hide()
            self._advanced_nav_visible = False

    def _init_theme(self) -> None:
        """Start dark on first run; afterwards honour the persisted theme."""
        setThemeColor("#0078D4")
        if not os.path.isfile(_CONFIG_FILE):
            setTheme(Theme.DARK)
            qconfig.save()

    def _toggle_theme(self) -> None:
        setTheme(Theme.LIGHT if qconfig.theme == Theme.DARK else Theme.DARK)

    # -- drag & drop -------------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        usable = [p for p in paths if p and os.path.exists(p)]
        if not usable:
            return
        if len(usable) > 1:
            self.decompile_page.log_card.append(
                f"拖入了 {len(usable)} 项；使用第一项（{os.path.basename(usable[0])}）。"
            )
        path = usable[0]
        if os.path.isfile(path) and path.lower().endswith('.vpk'):
            self.weapon_conversion_page.set_input_path(path)
            self.switchTo(self.weapon_conversion_page)
            event.acceptProposedAction()
            return
        kind = classify_dropped_path(path)
        if kind == "folder":
            self.decompile_page.mode_box.setCurrentIndex(1)
        else:
            self.decompile_page.mode_box.setCurrentIndex(0)
        self.decompile_page.set_input_path(path)
        self.decompile_page.log_card.append(
            f"Dropped {'folder' if kind == 'folder' else 'MDL file'}: {path}"
        )

    # -- input syncing ------------------------------------------------------
    def _sync_inputs(self) -> None:
        """Keep the decompile and one-click input boxes in sync."""
        decompile_edit = self.decompile_page.input_edit
        one_click_edit = self.one_click_page.input_edit

        def bridge(source_edit, target_edit):
            def on_change(text: str) -> None:
                if target_edit.text() != text:
                    target_edit.setText(text)
            source_edit.textChanged.connect(on_change)

        bridge(decompile_edit, one_click_edit)
        bridge(one_click_edit, decompile_edit)
        for edit in (decompile_edit, one_click_edit):
            edit.textChanged.connect(self._update_path_labels)
        self._update_path_labels(decompile_edit.text())

    def _update_path_labels(self, text: str) -> None:
        """Show the synced input path (read-only) on the tuning pages."""
        path = (text or "").strip()
        display = path if path else "未选择"
        self.subtract_page.path_label.setText("当前输入: " + display)
        self.bodygroup_page.path_label.setText("当前输入: " + display)

    # -- run orchestration --------------------------------------------------
    def _worker_busy(self) -> bool:
        # A finished thread remains active until its queued completion reaches Qt.
        return self._active_run_page is not None or (
            self._worker is not None and self._worker.is_alive())

    def run_weapon_scan(self, page, request=None):
        if self._worker_busy():
            return False
        request=request or (page._scan_generation,page.scan_path())
        if request[1] is None or not page.scan_is_current(request):return False
        page.cancel_pending_scan()
        path = request[1]
        page.invalidate_package()
        page.log_card.clear();page.begin_scan()
        signals=self._signals;cancel_event=self._cancel_event

        def work():
            try:
                signals.log.emit('[阶段] 解析 VPK 和来源武器')
                result = inspect_weapon_package(path)
                if cancel_event.is_set():
                    signals.weapon_scan_outcome.emit((request,None,'解析已取消。'))
                    signals.done.emit('解析已取消。')
                    return
                signals.weapon_scan_outcome.emit((request,result,''))
                signals.done.emit('解析完成：'+result.weapon.name)
            except UnsupportedViewModelError as error:
                signals.weapon_scan_outcome.emit((request,None,error))
                signals.done.emit(str(error))
            except Exception as error:
                signals.weapon_scan_outcome.emit((request,None,exception_text(error)))
                signals.done.emit('解析失败：'+exception_text(error))
        self._start_weapon_worker(page,work,'解析 VPK',scan_request=request)
        self._active_scan_request=request
        return True

    def run_weapon_conversion(self, page):
        if self._worker_busy():
            return
        if not self._leave_correction_draft():return
        try:
            target=page.target_box.currentData()
            if page.package is None or page.scan_path()!=page.package.path or not target:
                raise ValueError('请选择已解析的来源 VPK 和有效目标武器')
            output=plan_output(page.package.path,target,page.output_edit.text())
            if output.exists():raise FileExistsError('结果文件已存在，请更改名称：'+str(output))
        except (OSError,ValueError) as error:
            page.log_card.append('转换未开始：'+str(error));page.update_preview();return
        options = ConversionOptions(Path(page.input_edit.text().strip()),page.target_box.currentData(),
                                    page.output_edit.text(),bool(CFG.get(CFG.advanced) and page.keep_temp_check.isChecked()),
                                    (CFG.game_path.value or '').strip(),keep_ads=page.ads_check.isChecked(),
                                    isolate_materials=page.material_check.isChecked(),compiler_path=(CFG.studiomdl_path.value or '').strip(),
                                    l4n_enabled=page.l4n_check.isChecked(),normalize_vmt=page.normalize_vmt_check.isChecked(),
                                    audio_logic=page.audio_logic_box.currentData(),ik_rule_handling=page.ik_box.currentData(),
                                    melee_layer_fadein_fix=page.fadein_check.isChecked(),source_pistol_mode=page.source_pistol_mode(),
                                    fps_match=bool(CFG.get(CFG.advanced) and page.fps_check.isChecked()),
                                    exclude_inspect_fps=bool(CFG.get(CFG.advanced) and page.inspect_fps_check.isChecked()))
        page.log_card.clear();page.begin_conversion(options)

        def work():
            try:
                result = convert_weapon_package(options,
                    on_stage=lambda m:self._signals.log.emit('[阶段] '+m),
                    on_detail=lambda m:self._signals.log.emit('[详情] '+m),
                    cancelled=self._cancel_event.is_set)
                self._signals.weapon_result.emit(result)
                if result.success:
                    summary = (f'转换完成：{result.output_path}；{len(result.compiled_models)} 个目标模型，'
                               f'{len(result.converted_audio_files)} 个目标 WAV，'
                               f'{len(result.converted_material_files)} 个材质/HUD 文件。')
                    if result.l4n_published:
                        summary += f' L4N 音频包：{result.l4n_output_path}；{len(result.l4n_audio_files)} 个 L4N WAV。'
                    elif options.l4n_enabled:
                        summary += ' 没有过剩音频，仅生成主 VPK。'
                elif result.unsupported_view_model:summary=result.error
                elif result.cancelled:summary='转换已取消。'+result.error
                else:summary='转换失败：'+result.error
                if result.work_dir:summary += ' 临时文件：'+str(result.work_dir)
                elif result.success:summary += ' 临时文件已清理。'
                if result.warnings:
                    summary += f' {len(dict.fromkeys(result.warnings))} 项提示，详情见高级日志。'
                self._signals.done.emit(summary)
            except Exception as error:
                self._signals.done.emit('转换失败：'+exception_text(error))
        self._start_weapon_worker(page,work,'武器转模')

    def _start_weapon_worker(self,page,work,status,*,scan_request=None):
        correction=self.action_correction_page
        bundle=correction.suspend_preview() if correction is not None else ([],[])
        self._preview_backdrop.invalidate()
        def prepared_work():
            try:
                from ..action_resources import release_preview_resources
                release_preview_resources(bundle)
                if self._cancel_event.is_set():
                    if scan_request is not None:
                        self._signals.weapon_scan_outcome.emit((scan_request,None,'解析已取消。'))
                    self._signals.done.emit('准备已取消。');return
                work()
            except Exception as error:
                if scan_request is not None:
                    self._signals.weapon_scan_outcome.emit((scan_request,None,exception_text(error)))
                self._signals.done.emit('预览资源卸载失败：'+exception_text(error))
        self._start_worker(page,prepared_work,status)

    def _ensure_action_correction_page(self):
        if self.action_correction_page is None:
            from .action_correction import ActionCorrectionInterface
            page=ActionCorrectionInterface(self,config=CFG)
            self.action_correction_page=page;self.stackedWidget.addWidget(page)
            self._preview_backdrop.attach(page.viewport);self._preview_backdrop.attach(page.official_viewport)
            page.back_requested.connect(self.clear_action_preview_and_return)
            page.save_requested.connect(self.save_action_correction)
        return self.action_correction_page

    def clear_action_preview_and_return(self):
        if self._worker_busy():return
        page=self.action_correction_page
        if page is None:return
        self._preview_resume_requested=False
        bundle=page.suspend_preview()
        self._preview_backdrop.invalidate()
        def work():
            try:
                from ..action_resources import release_preview_resources
                release_preview_resources(bundle)
            except Exception as error:
                self._signals.correction_failed.emit(exception_text(error))
                self._signals.done.emit('预览资源卸载失败：'+exception_text(error));return
            self._signals.correction_preview_cleared.emit()
            self._signals.done.emit('预览资源已清空。')
        self._start_worker(page,work,'清空动作预览')
        # Disposal must finish once detached; it cannot restore a half-cleared preview.
        page.cancel_button.hide()

    def _on_correction_preview_cleared(self):
        self._preview_resume_requested=False
        self.switchTo(self.weapon_conversion_page)

    def show_action_correction(self):
        page=self._ensure_action_correction_page()
        self._preview_resume_requested=page.suspended
        self.switchTo(page)
        self._resume_action_preview()

    def _resume_action_preview(self):
        page=self.action_correction_page
        if (not getattr(self,"_preview_resume_requested",False) or page is None or not page.suspended
                or self.stackedWidget.currentWidget() is not page
                or self.weapon_conversion_page._pending_scan is not None):return
        if self._worker_busy():
            # Queued done can arrive while the worker is still returning.
            if self._active_run_page is None:QTimer.singleShot(25,self._resume_action_preview)
            return
        self._preview_resume_requested=False
        session=page.session
        def work():
            try:
                if getattr(session,'standalone',False):
                    payload=session.load_preview_assets(cancelled=self._cancel_event.is_set)
                else:
                    from ..action_assets import load_preview_assets
                    payload=load_preview_assets(session,cancelled=self._cancel_event.is_set)
                from ..action_compare import with_original_assets
                payload=with_original_assets(payload,cancelled=self._cancel_event.is_set)
                if self._cancel_event.is_set():raise InterruptedError('预览载入已取消。')
                self._signals.correction_loaded.emit(payload)
                self._signals.done.emit('动作预览已恢复。')
            except Exception as error:
                # A partially read model must not survive a cancelled restore.
                session._preview_models.clear()
                self._signals.correction_failed.emit(exception_text(error))
                self._signals.done.emit('动作预览恢复失败：'+exception_text(error))
        self._start_worker(page,work,'恢复动作预览')

    def choose_action_model(self):
        if self._worker_busy() or not self._leave_correction_draft():return
        model_path,_=QFileDialog.getOpenFileName(self,'选择需要矫正的 V 模','','Source 模型 (*.mdl)')
        if not model_path:return
        from .action_correction import choose_model_profile
        profile_id=choose_model_profile(self,model_path)
        if profile_id is None:return
        self.open_standalone_correction(model_path,profile_id)

    def open_standalone_correction(self,model_path,profile_id):
        if self._worker_busy():return
        page=self._ensure_action_correction_page()
        if not self._leave_correction_draft():return
        page.clear_loaded();self.switchTo(page)
        game_path=(CFG.game_path.value or '').strip();compiler=(CFG.studiomdl_path.value or '').strip()
        def work():
            try:
                from ..action_standalone import StandaloneCorrectionSession
                session=StandaloneCorrectionSession.from_model(model_path,profile_id,game_path=game_path,compiler_path=compiler)
                payload=session.load_preview_assets(cancelled=self._cancel_event.is_set)
                from ..action_compare import with_original_assets
                payload=with_original_assets(payload,cancelled=self._cancel_event.is_set)
                if self._cancel_event.is_set():raise InterruptedError('模型载入已取消。')
                self._signals.correction_loaded.emit(payload);self._signals.done.emit('V 模已载入；保存时准备可编辑动作源。')
            except Exception as error:
                self._signals.correction_failed.emit(exception_text(error));self._signals.done.emit('V 模载入失败：'+exception_text(error))
        self._start_worker(page,work,'载入待矫正 V 模')

    def open_action_correction(self, conversion_page):
        if self._worker_busy():return
        result,options=conversion_page.correction_result,conversion_page.correction_options
        if result is None or options is None:return
        if not result.work_dir or not Path(result.work_dir).is_dir() or not Path(result.output_path).is_file():
            conversion_page.invalidate_correction()
            conversion_page.log_card.append('成品或保留临时文件已丢失，无法进入动作矫正。')
            return
        page=self._ensure_action_correction_page()
        cached=page.session if page.session and getattr(page.session,'result',None) is result and page.session.options==options else None
        if page.session and cached is None and not self._leave_correction_draft():return
        if cached is not None and page.suspended:
            self.show_action_correction();return
        if self.weapon_conversion_page._pending_scan is not None:return
        if cached is None:page.clear_loaded()
        self.switchTo(page)
        def work():
            try:
                from ..action_session import CorrectionSession
                from ..action_assets import load_preview_assets
                session=cached or CorrectionSession.from_result(result,options)
                if cached:
                    session.check_unchanged()
                if cached and page._loaded_models is session.models:
                    payload=(session,page._previews,page._materials)
                else:payload=load_preview_assets(session,cancelled=self._cancel_event.is_set)
                from ..action_compare import with_original_assets
                payload=with_original_assets(payload,cancelled=self._cancel_event.is_set)
                if self._cancel_event.is_set():raise InterruptedError('预览载入已取消。')
                self._signals.correction_loaded.emit(payload)
                self._signals.done.emit('成品动作预览已载入。')
            except Exception as error:
                self._signals.correction_failed.emit(exception_text(error))
                self._signals.done.emit('动作矫正载入失败：'+exception_text(error))
        self._start_worker(page,work,'载入成品动作预览')

    def _on_correction_loaded(self,payload):
        page=self.action_correction_page
        if page is None:return
        page.set_loaded(payload)

    def _on_correction_failed(self,message):
        page=self.action_correction_page
        if page is None:return
        page.log_card.append(message)
        page.status_label.setText(message)

    def _on_correction_save_finished(self,outcome):
        status,path=outcome
        success=status=='success'
        title='动作矫正成功' if success else '动作矫正已取消' if status=='cancelled' else '动作矫正失败'
        content='成品已保存：'+str(path) if success else '操作已取消。' if status=='cancelled' else '请查看处理日志。'
        show=InfoBar.success if success else InfoBar.error
        show(title,content,isClosable=False,duration=3000,
             position=InfoBarPosition.TOP_RIGHT,parent=self)

    def save_action_correction(self):
        page=self.action_correction_page
        if page is None or page.session is None or self._worker_busy():return
        page.commit_fps_edit()
        if not page.session.changes:return
        session=page.session
        standalone=bool(getattr(session,'standalone',False))
        output_dir=getattr(session,'output_dir',None)
        if standalone and not output_dir:
            output_dir=QFileDialog.getExistingDirectory(self,'选择新的空目录以保存矫正模型',str(Path(session.models[0].model_path).parent))
            if not output_dir:
                self._on_correction_save_finished(('cancelled',None))
                return
        def work():
            try:
                kwargs={'output_dir':output_dir} if standalone else {}
                saved=session.save(cancelled=self._cancel_event.is_set,on_log=self._signals.log.emit,**kwargs)
            except Exception as error:
                cancelled=isinstance(error,InterruptedError)
                self._signals.correction_save_finished.emit(('cancelled' if cancelled else 'failed',None))
                self._signals.correction_failed.emit(exception_text(error))
                self._signals.done.emit(('动作矫正保存已取消：' if cancelled else '动作矫正保存失败：')+exception_text(error))
                return
            path=getattr(saved,'path',None) or getattr(session,'output_path',None) or output_dir
            self._signals.correction_save_finished.emit(('success',path))
            # Publication is already committed. A preview reload problem must
            # be reported as a refresh problem, never as an unsuccessful save.
            try:
                from ..action_assets import load_preview_assets
                # Always read the newly published MDL. A pre-save parsed model
                # must not supply sequence descriptors after a permutation.
                payload=(session.load_preview_assets(reload_models=True) if standalone else
                         load_preview_assets(session,reload_models=True))
                from ..action_compare import with_original_assets
                payload=with_original_assets(payload)
                self._signals.correction_loaded.emit(payload)
                self._signals.done.emit('动作矫正已保存，编译模型及必要伴随文件已导出：'+str(path)
                                       if standalone else '动作矫正已保存，成品 QC 和 VPK 已更新。')
            except Exception as error:
                self._signals.correction_failed.emit('成品已保存，预览刷新失败：'+exception_text(error))
                self._signals.done.emit('成品已保存；请重新进入以刷新预览。')
        self._start_worker(page,work,'保存动作矫正')

    def _leave_correction_draft(self):
        page=self.action_correction_page
        if page is None or page.session is None:return True
        page.commit_fps_edit()
        if not page.session.changes:return True
        from .action_dialogs import UnsavedActionDialog
        box=UnsavedActionDialog(self);box.exec()
        if box.choice=='discard':page.session.discard();return True
        if box.choice=='save':
            self.switchTo(page);page._confirm_save()
        return False

    def closeEvent(self,event):
        if self.action_correction_page is not None and self._active_run_page is self.action_correction_page:
            self.action_correction_page.log_card.append('请先等待当前动作任务完成，或取消任务后再关闭。')
            event.ignore();return
        if not self._leave_correction_draft():event.ignore();return
        if self.action_correction_page is not None:
            self.action_correction_page.pause()
            self.action_correction_page.audio.close()
            self.action_correction_page.viewport.release_resources()
            self.action_correction_page.official_viewport.release_resources()
        super().closeEvent(event)

    def _collect_targets(self, input_path: str, mode_index: int, output_path: str = "") -> list[str]:
        input_path = input_path.strip()
        if not input_path:
            InfoBar.warning("反编译", "请先选择输入的 .mdl 文件或文件夹。", duration=3000,
                            parent=self, position=InfoBarPosition.TOP)
            return []
        if os.path.isfile(input_path):
            return [input_path]
        if not os.path.isdir(input_path):
            InfoBar.error("反编译", f"路径不存在：\n{input_path}", duration=3000,
                          parent=self, position=InfoBarPosition.TOP)
            return []
        targets: list[str] = []
        excluded = os.path.normcase(os.path.realpath(output_path)) if output_path else ""
        if excluded == os.path.normcase(os.path.realpath(input_path)):
            excluded = ""
        if mode_index == 1:  # Folder
            for name in sorted(os.listdir(input_path)):
                full = os.path.join(input_path, name)
                if os.path.isfile(full) and name.lower().endswith(".mdl"):
                    targets.append(full)
        else:  # Folder and subfolders
            for root, _dirs, names in os.walk(input_path):
                _dirs[:] = [d for d in _dirs if d.lower() not in ("source", "dec")
                            and not d.startswith(".loka_compile_")
                            and os.path.normcase(os.path.realpath(os.path.join(root, d))) != excluded
                            and not is_compile_bundle(os.path.join(root, d))]
                for name in sorted(names):
                    if name.lower().endswith(".mdl"):
                        targets.append(os.path.join(root, name))
        if not targets:
            # NOTE: parented to the window, not to the decompile page: this path
            # runs from the one-click page too, and the decompile page is hidden
            # whenever advanced mode is off -- a hidden parent hides the InfoBar.
            InfoBar.warning("反编译", "该文件夹内没有 .mdl 文件。", duration=3000,
                            parent=self, position=InfoBarPosition.TOP)
        return targets

    def run_decompile_page(self, page: DecompileInterface) -> None:
        if self._worker_busy():
            return
        input_path = page.input_edit.text()
        output_base = page.output_edit.text().strip()
        targets = self._collect_targets(input_path, page.mode_box.currentIndex(), output_base)
        if not targets:
            return
        options = page.collect_options()

        self._start_worker(
            page,
            self._work_decompile(targets, options, output_base),
            f"Decompiling {len(targets)} model(s) ...",
        )

    def run_one_click_page(self, page: OneClickInterface) -> None:
        if self._worker_busy():
            return
        game_path = (CFG.game_path.value or "").strip()
        exe_path = (CFG.studiomdl_path.value or "").strip()
        if not game_path and not exe_path:
            InfoBar.warning(
                "兼容ADS处理", "请先在「设置」页配置游戏路径或编译器路径。",
                duration=4000, parent=page, position=InfoBarPosition.TOP,
            )
            return
        input_path = page.input_edit.text().strip()
        targets = self._collect_targets(input_path, 0 if os.path.isfile(input_path) else 2)
        if not targets:
            return

        options = self.decompile_page.collect_options()
        if page.fadein_check.isChecked():
            options.melee_layer_fadein_fix = True
        self._start_worker(
            page,
            self._work_one_click(targets, options, exe_path, game_path),
            f"兼容ADS处理 {len(targets)} model(s) ...",
        )

    def _start_worker(self, page, work, status: str) -> None:
        if self._worker_busy():
            return
        self._active_run_page = page
        self._active_scan_request = None
        self._cancel_event.clear()
        if hasattr(page,'set_busy'):page.set_busy(True)
        page.run_button.setEnabled(False)
        if hasattr(page, "cancel_button"):
            page.cancel_button.setEnabled(True)
        page.progress.start();page.progress.show()
        if getattr(page,'_conversion_active',False):page.progress.hide()
        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    # worker bodies (run inside a plain thread; only emit signals)
    def _work_decompile(self, targets, options, output_base):
        def work() -> None:
            # emit the MainWindow's signals: connected in the GUI thread, so
            # emissions from this worker thread are queued back safely
            signals = self._signals

            succeeded = failed = 0
            root = os.path.commonpath([os.path.dirname(os.path.abspath(p)) for p in targets])

            def emit(message: str) -> None:
                signals.log.emit("    " + message)

            for index, mdl_path in enumerate(targets, start=1):
                if self._cancel_event.is_set():
                    signals.log.emit("已取消。")
                    break
                signals.log.emit(f"[{index}/{len(targets)}] {os.path.basename(mdl_path)}")

                relative = os.path.relpath(os.path.dirname(mdl_path), root)
                if not output_base:
                    output_path = os.path.join(os.path.dirname(os.path.abspath(mdl_path)), "dec")
                else:
                    output_path = (
                        output_base if relative in (".", "")
                        else os.path.join(output_base, relative)
                    )
                if len(targets) > 1:
                    output_path = os.path.join(output_path, os.path.splitext(os.path.basename(mdl_path))[0])
                try:
                    decompiler = MdlDecompiler(options)
                    result = decompiler.decompile(mdl_path, output_path, progress=emit)
                except Exception as error:  # noqa: BLE001
                    signals.log.emit(f"    错误: {exception_text(error)}")
                    failed += 1
                    continue
                for warning in result.warnings:
                    signals.log.emit(f"    警告: {warning}")
                for error in result.errors:
                    signals.log.emit(f"    错误: {error}")
                if result.success:
                    succeeded += 1
                    signals.log.emit(
                        f"    已写入 {len(result.written_files)} 个文件到 {output_path}"
                    )
                else:
                    failed += 1
            signals.done.emit(f"完成。成功 {succeeded} 个，失败 {failed} 个。")
        return work

    def _work_one_click(self, targets, options, exe_path, game_path):
        def work() -> None:
            signals = self._signals
            succeeded = failed = 0
            for index, mdl_path in enumerate(targets, start=1):
                if self._cancel_event.is_set():
                    signals.log.emit("已取消。")
                    break
                signals.log.emit(f"[{index}/{len(targets)}] {os.path.basename(mdl_path)}")
                outcome = run_one_click(
                    mdl_path, options,
                    studiomdl_path=exe_path,
                    game_path=game_path,
                    output_path=os.path.join(os.path.dirname(mdl_path), "dec", os.path.splitext(os.path.basename(mdl_path))[0]) if len(targets) > 1 else None,
                    log=lambda message: signals.log.emit("    " + message),
                )
                if outcome.succeeded:
                    succeeded += 1
                else:
                    failed += 1
            signals.done.emit(f"完成。成功 {succeeded} 个，失败 {failed} 个。")
        return work

    # worker -> UI (queued)
    def _on_worker_log(self, message: str) -> None:
        if self._active_run_page is not None:
            if self._active_scan_request is not None and not self.weapon_conversion_page.scan_is_current(self._active_scan_request):return
            self._active_run_page.log_card.append(message)
            if hasattr(self._active_run_page,'on_stage_progress'):self._active_run_page.on_stage_progress(message)

    def _on_worker_done(self, summary: str) -> None:
        page = self._active_run_page
        if page is not None:
            if self._active_scan_request is None or self.weapon_conversion_page.scan_is_current(self._active_scan_request):
                page.log_card.append(summary)
                if hasattr(page,'finish_worker'):page.finish_worker(summary)
            page.run_button.setEnabled(True)
            if hasattr(page, "cancel_button"):
                page.cancel_button.setEnabled(False)
            page.progress.stop()
            page.progress.hide()
            if hasattr(page,'set_busy'):page.set_busy(False)
        self._active_run_page = None
        self._active_scan_request = None
        QTimer.singleShot(0,self.weapon_conversion_page.dispatch_pending_scan)
        # A request made during weapon work restores only while this page is visible.
        QTimer.singleShot(0,self._resume_action_preview)
        self.setWindowTitle(f"Loka {__version__}")


# one-click option shim: reuse DecompileOptions without polluting the dataclass
def _patch_one_click_options():
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the Loka GUI")
    parser.set_defaults(preview_artwork=None)
    if not getattr(sys, 'frozen', False):
        parser.add_argument("--preview-artwork", choices=PREVIEW_CHOICES,
                            help="Open artwork tuning for a selected group (1-6 or night)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    app = QApplication([sys.argv[0]])
    try:
        window = MainWindow(preview_artwork=args.preview_artwork)
    except FileNotFoundError as error:
        parser.error(str(error))
    window.show()
    return app.exec()


if __name__ == "__main__":
    from loka.process_limits import run_with_process_memory_limit

    raise SystemExit(run_with_process_memory_limit(main))
