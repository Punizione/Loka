"""Modal, bounded GUI orchestration for user-initiated update checks."""
import threading
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QTimer, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QVBoxLayout
from qfluentwidgets import BodyLabel, IndeterminateProgressBar, SubtitleLabel, isDarkTheme

from ..i18n import tr
from ..update_check import CHECK_TIMEOUT, RELEASES_URL, UpdateCheckError, check_update, validate_proxy
from .action_dialogs import ActionFormDialog


_ERROR_MESSAGES = {
    'proxy': '代理地址无效，请填写 http://主机:端口 或 socks5://主机:端口。',
    'source': '请在打包后的 Loka exe 中检查更新。',
    'local_file': '无法读取当前 exe，检查更新失败。',
    'timeout': '检查更新超时，请稍后重试或配置代理。',
    'tls': '安全连接失败，请检查系统时间、证书或代理设置。',
    'network': '无法连接更新服务器，请检查网络或代理设置。',
    'server': '更新服务器暂不可用，请稍后重试。',
    'rate_limit': '更新请求过于频繁，请稍后重试。',
    'metadata': '发布附件或 SHA256 信息不完整，无法检查更新，请通过更新链接查看。',
}


class UpdateProgressDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent, Qt.Dialog | Qt.FramelessWindowHint)
        self.setWindowModality(Qt.ApplicationModal)
        self.setWindowTitle(tr('检查更新'))
        self.setFixedWidth(440)
        self.setStyleSheet('QDialog { background: #202020; }' if isDarkTheme()
                           else 'QDialog { background: #fafafa; }')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(SubtitleLabel(tr('正在检查更新'), self))
        message = BodyLabel(tr('检查期间请稍候，其他功能暂不可用。'), self)
        message.setWordWrap(True)
        layout.addWidget(message)
        layout.addWidget(IndeterminateProgressBar(self))

    def reject(self):
        # Escape/Alt+F4 cannot unlock the main window while a check is active.
        pass

    def closeEvent(self, event):
        event.ignore()


class UpdateCheckController(QObject):
    notified = Signal(str, str)
    _finished = Signal(int, object)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.active = False
        self.dialog = None
        self.available_dialog = None
        self.thread = None
        self._generation = 0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(CHECK_TIMEOUT * 1000)
        self.timer.timeout.connect(lambda: self._complete(UpdateCheckError('timeout')))
        self._finished.connect(self._on_finished)

    def start(self, proxy_url):
        if self.active or self.available_dialog is not None:
            return
        try:
            validate_proxy(proxy_url)
        except UpdateCheckError as error:
            self.notified.emit(tr(_ERROR_MESSAGES[error.code]), 'error')
            return
        self.active = True
        self._generation += 1
        generation = self._generation
        self._locked_widgets = [(widget, widget.isEnabled()) for widget in (
            self.window.navigationInterface, self.window.stackedWidget, self.window.titleBar)]
        for widget, _enabled in self._locked_widgets:
            widget.setEnabled(False)
        self._accept_drops = self.window.acceptDrops()
        self.window.setAcceptDrops(False)
        self.dialog = UpdateProgressDialog(self.window)
        self.dialog.show()
        self.timer.start(CHECK_TIMEOUT * 1000)
        finished = self._finished

        def work():
            try:
                outcome = check_update(proxy_url=proxy_url, timeout=CHECK_TIMEOUT)
            except UpdateCheckError as error:
                outcome = error
            except Exception:
                outcome = UpdateCheckError('network')
            try:
                finished.emit(generation, outcome)
            except RuntimeError:
                # A DNS call may outlive the GUI deadline and application shutdown.
                pass

        self.thread = threading.Thread(target=work, name='Loka-update-check', daemon=True)
        self.thread.start()

    def _on_finished(self, generation, outcome):
        if self.active and generation == self._generation:
            self._complete(outcome)

    def _complete(self, outcome):
        if not self.active:
            return
        self.timer.stop()
        self.active = False
        self.dialog.hide()
        self.dialog.deleteLater()
        self.dialog = None
        for widget, enabled in self._locked_widgets:
            widget.setEnabled(enabled)
        self.window.setAcceptDrops(self._accept_drops)
        if isinstance(outcome, UpdateCheckError):
            self.notified.emit(tr(_ERROR_MESSAGES.get(outcome.code, _ERROR_MESSAGES['network'])), 'error')
        elif outcome.update_available:
            dialog = UpdateAvailableDialog(outcome, self.window)
            self.available_dialog = dialog
            dialog.finished.connect(self._available_closed)
            dialog.show()
        else:
            self.notified.emit(tr('当前 exe 与最新发布文件一致，无需更新。'), 'success')

    def _available_closed(self, _result):
        self.available_dialog.deleteLater()
        self.available_dialog = None


def _publication_time(value):
    try:
        published = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if published.utcoffset() is not None:
            return published.astimezone().strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError, AttributeError, OverflowError):
        pass
    return tr('未知')


class UpdateAvailableDialog(ActionFormDialog):
    def __init__(self, result, parent):
        super().__init__('发现新版本', parent, confirm_text='前往更新页', width=480)
        self.setWindowModality(Qt.ApplicationModal)
        version = BodyLabel(tr('新版本：{version}', version=result.latest_version or tr('未知')), self.widget)
        published = BodyLabel(tr('发布时间（本地时间）：{time}', time=_publication_time(result.published_at)), self.widget)
        for label in (version, published):
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            self.viewLayout.addWidget(label)
        self.validate_callback = lambda: QDesktopServices.openUrl(QUrl(RELEASES_URL))
