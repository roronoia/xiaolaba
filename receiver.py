import sys
import os
import json
import time
import subprocess
import queue
import threading
import winreg
import winsound
import ctypes
from ctypes import wintypes
import qrcode

import paho.mqtt.client as mqtt
import win32com.client
import win32gui
import win32con
import pythoncom

from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, QObject, QPropertyAnimation,
    QEasingCurve, QPoint
)
from PyQt6.QtGui import QPixmap, QImage, QColor, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QGraphicsDropShadowEffect,
    QLineEdit, QCheckBox, QSystemTrayIcon, QMenu,
    QComboBox, QSlider, QProgressBar, QScrollArea,
    QDialog, QTabWidget
)

# ----------------- Windows UAC 管理员权限检测与自动提权 -----------------
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def ensure_admin():
    if "--no-uac" in sys.argv or os.environ.get("SKIP_UAC") == "1" or "--guardian" in sys.argv:
        return
    if not is_admin():
        try:
            executable = sys.executable
            if getattr(sys, 'frozen', False):
                params = " ".join([f'"{arg}"' for arg in sys.argv[1:]])
                ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
            else:
                script_file = os.path.abspath(sys.argv[0])
                args = f'"{script_file}" ' + " ".join([f'"{arg}"' for arg in sys.argv[1:]])
                ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, args.strip(), None, 1)
            if ret > 32:
                sys.exit(0)
        except Exception as e:
            print("Ensure admin error:", e)

# ----------------- 系统级高斯模糊 (DWM Blur Behind) -----------------
class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [
        ('AccentState', wintypes.DWORD),
        ('AccentFlags', wintypes.DWORD),
        ('GradientColor', wintypes.DWORD),
        ('AnimationId', wintypes.DWORD)
    ]

class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
    _fields_ = [
        ('Attribute', wintypes.DWORD),
        ('Data', ctypes.c_void_p),
        ('SizeOfData', ctypes.c_size_t)
    ]

def enable_acrylic_blur(hwnd, is_dark=True):
    try:
        user32 = ctypes.windll.user32
        accent = ACCENT_POLICY()
        accent.AccentState = 3
        accent.AccentFlags = 2
        accent.GradientColor = 0x881a202c if is_dark else 0x88f8fafc

        data = WINDOWCOMPOSITIONATTRIBDATA()
        data.Attribute = 19
        data.Data = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
        data.SizeOfData = ctypes.sizeof(accent)

        user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
    except Exception as e:
        print("Enable acrylic blur error:", e)


if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
ICON_PATH = os.path.join(BASE_DIR, "app_icon.ico")
CHIME_PATH = os.path.join(BASE_DIR, "chime.wav")
AUTH_EXIT_FILE = os.path.join(BASE_DIR, ".authorized_exit")

DEFAULT_CONFIG = {
    "room_id": "classroom_703",
    "secret_token": "123456",  # 默认教师发送口令 (手机端需一致)
    "admin_password": "888888",  # 默认管理员密码 (支持在系统设置中修改)
    "pages_url": "",           # Cloudflare Pages 专属访问网址 (必需配置)
    "font_size_preset": "large",  # normal / large (默认教学大屏) / xlarge
    "voice_index": 0,
    "voice_rate": 0,              # 语速默认 0
    "voice_volume": 100,
    "play_chime": True,
    "dnd_mode": False,
    "banner_duration": 10,
    "auto_start": True,           # 默认开机静默启动
    "watchdog_enabled": True      # 默认开启双进程防杀自愈守护
}

# ----------------- 双进程看护机制 (防止任务管理器强杀) -----------------
def spawn_gui(is_recovered=False, guardian_pid=None):
    if getattr(sys, 'frozen', False):
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, os.path.abspath(__file__)]
    if is_recovered:
        cmd.append("--recovered")
    if guardian_pid:
        cmd.extend(["--guardian-pid", str(guardian_pid)])
    try:
        DETACHED_PROCESS = 0x00000008
        return subprocess.Popen(cmd, creationflags=DETACHED_PROCESS)
    except Exception as e:
        print("Spawn GUI error:", e)
        return None

def run_guardian(gui_pid):
    # 守护进程：无UI后台静默运行，0% CPU系统开销
    SYNCHRONIZE = 0x00100000
    current_pid = int(gui_pid)
    while True:
        h_gui = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, current_pid)
        if h_gui:
            # 阻塞挂起，等待被监控的主进程终止 (由系统内核驱动唤醒，零轮询)
            ctypes.windll.kernel32.WaitForSingleObject(h_gui, 0xFFFFFFFF)
            ctypes.windll.kernel32.CloseHandle(h_gui)

        time.sleep(0.3)
        # 检查是否为主程序通过密码验证的合法退出
        if os.path.exists(AUTH_EXIT_FILE):
            try:
                os.remove(AUTH_EXIT_FILE)
            except Exception:
                pass
            break

        cfg = load_config()
        if not cfg.get("watchdog_enabled", True):
            break

        # 非授权异常退出（如任务管理器结束进程），立即拉起复活主进程
        new_proc = spawn_gui(is_recovered=True, guardian_pid=os.getpid())
        if new_proc:
            current_pid = new_proc.pid
        else:
            time.sleep(1)

MQTT_BROKER = "broker-cn.emqx.io"
MQTT_PORT = 1883

FONT_PRESETS = {
    "normal": {
        "name_size": 34, "content_size": 26, "badge_size": 14,
        "btn_size": 13, "time_size": 13, "width": 900, "height": 220
    },
    "large": {  # 教学大屏推荐
        "name_size": 44, "content_size": 32, "badge_size": 16,
        "btn_size": 14, "time_size": 15, "width": 1020, "height": 265
    },
    "xlarge": {  # 远距离超大号
        "name_size": 54, "content_size": 38, "badge_size": 18,
        "btn_size": 16, "time_size": 16, "width": 1140, "height": 310
    }
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                merged = {**DEFAULT_CONFIG, **cfg}
                if not merged.get("admin_password"):
                    merged["admin_password"] = "888888"
                return merged
        except Exception as e:
            print("Load config error:", e)
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Save config error:", e)


def set_autostart(enable=True):
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "BlackboardSpeaker"
    exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
        if enable:
            if getattr(sys, 'frozen', False):
                cmd = f'"{exe_path}" --tray'
            else:
                cmd = f'"{sys.executable}" "{exe_path}" --tray'
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, app_name)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print("Set autostart error:", e)


# ----------------- 语音与音频队列 -----------------
class VoiceService:
    _queue = queue.Queue()
    _worker_started = False
    _lock = threading.Lock()
    _current_cfg = DEFAULT_CONFIG.copy()

    @classmethod
    def update_config(cls, cfg):
        with cls._lock:
            cls._current_cfg.update(cfg)

    @classmethod
    def play_chime(cls):
        with cls._lock:
            dnd = cls._current_cfg.get("dnd_mode", False)
            chime = cls._current_cfg.get("play_chime", True)
        if not dnd and chime and os.path.exists(CHIME_PATH):
            try:
                winsound.PlaySound(CHIME_PATH, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception as e:
                print("Play chime error:", e)

    @classmethod
    def speak(cls, text, repeat=1):
        cls._ensure_worker()
        cls._queue.put((text, repeat))

    @classmethod
    def _ensure_worker(cls):
        if not cls._worker_started:
            cls._worker_started = True
            t = threading.Thread(target=cls._worker_loop, daemon=True)
            t.start()

    @classmethod
    def _worker_loop(cls):
        pythoncom.CoInitialize()
        speaker = None
        try:
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
        except Exception as e:
            print("Init SAPI failed:", e)

        while True:
            text, repeat = cls._queue.get()
            try:
                with cls._lock:
                    dnd = cls._current_cfg.get("dnd_mode", False)
                    chime = cls._current_cfg.get("play_chime", True)
                    v_idx = cls._current_cfg.get("voice_index", 0)
                    v_rate = cls._current_cfg.get("voice_rate", 0)
                    v_vol = cls._current_cfg.get("voice_volume", 100)

                if not dnd:
                    if chime and os.path.exists(CHIME_PATH):
                        try:
                            winsound.PlaySound(CHIME_PATH, winsound.SND_FILENAME)
                        except Exception as e:
                            print("Play chime error:", e)

                    if speaker:
                        if 0 <= v_idx < speaker.GetVoices().Count:
                            speaker.Voice = speaker.GetVoices().Item(v_idx)
                        speaker.Rate = v_rate
                        speaker.Volume = v_vol

                        for _ in range(repeat):
                            speaker.Speak(text)
                            time.sleep(0.4)
            except Exception as e:
                print("Voice playback error:", e)
            finally:
                cls._queue.task_done()


# ----------------- 现代毛玻璃微质感通知弹窗 (大字号增强版) -----------------
class BannerOverlay(QWidget):
    repeat_requested = pyqtSignal(str, str, str)

    def __init__(self, font_preset="large"):
        super().__init__()
        self.font_preset = font_preset
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self.current_student = ""
        self.current_content = ""
        self.total_duration_ms = 10000
        self.remaining_ms = 10000
        self.is_paused = False

        self.init_ui()

        self.tick_timer = QTimer(self)
        self.tick_timer.setInterval(100)
        self.tick_timer.timeout.connect(self.on_tick)

        self.topmost_timer = QTimer(self)
        self.topmost_timer.setInterval(1000)
        self.topmost_timer.timeout.connect(self.enforce_topmost)

    def set_font_preset(self, preset_name):
        if preset_name in FONT_PRESETS:
            self.font_preset = preset_name
            self.apply_font_styles()

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)

        self.card = QFrame(self)
        self.card.setObjectName("frostedCard")
        self.card.setStyleSheet("""
            QFrame#frostedCard {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 rgba(28, 36, 52, 0.78),
                    stop: 0.6 rgba(20, 26, 40, 0.85),
                    stop: 1 rgba(15, 20, 32, 0.90)
                );
                border: 1.5px solid rgba(255, 255, 255, 0.18);
                border-top: 1.5px solid rgba(255, 255, 255, 0.38);
                border-radius: 18px;
            }
        """)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(46)
        shadow.setColor(QColor(0, 0, 0, 85))
        shadow.setOffset(0, 14)
        self.card.setGraphicsEffect(shadow)

        self.card_layout = QVBoxLayout(self.card)
        self.card_layout.setContentsMargins(32, 22, 32, 18)
        self.card_layout.setSpacing(12)

        # 顶部栏
        self.top_layout = QHBoxLayout()

        self.badge = QLabel("📢 黑板小喇叭 · 教学广播")
        self.top_layout.addWidget(self.badge)

        self.time_label = QLabel()
        self.top_layout.addWidget(self.time_label)
        self.top_layout.addStretch()

        self.pause_hint = QLabel("(悬停暂停倒计时)")
        self.pause_hint.setStyleSheet("color: #38bdf8; font-size: 14px; font-weight: 600;")
        self.pause_hint.setVisible(False)
        self.top_layout.addWidget(self.pause_hint)

        self.reread_btn = QPushButton("🔁 重读一遍")
        self.reread_btn.clicked.connect(self.on_reread_clicked)
        self.top_layout.addWidget(self.reread_btn)

        self.close_btn = QPushButton("✕ 关闭")
        self.close_btn.clicked.connect(self.dismiss)
        self.top_layout.addWidget(self.close_btn)

        self.card_layout.addLayout(self.top_layout)

        # 学生名字标签
        self.student_label = QLabel()
        self.card_layout.addWidget(self.student_label)

        # 通知事由标签
        self.content_label = QLabel()
        self.content_label.setWordWrap(True)
        self.card_layout.addWidget(self.content_label)

        # 底部进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background: rgba(255, 255, 255, 0.10);
                border: none;
                border-radius: 2px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #38bdf8, stop:1 #818cf8);
                border-radius: 2px;
            }
        """)
        self.card_layout.addWidget(self.progress_bar)

        main_layout.addWidget(self.card)
        self.apply_font_styles()

    def apply_font_styles(self):
        p = FONT_PRESETS.get(self.font_preset, FONT_PRESETS["large"])

        # 药丸 Badge
        self.badge.setStyleSheet(f"""
            background: rgba(239, 68, 68, 0.22);
            border: 1.5px solid rgba(248, 113, 113, 0.45);
            color: #fca5a5;
            font-size: {p['badge_size']}px;
            font-weight: 700;
            padding: 4px 14px;
            border-radius: 12px;
        """)

        # 时间
        self.time_label.setStyleSheet(f"color: rgba(255, 255, 255, 0.6); font-size: {p['time_size']}px; font-weight: 600;")

        # 重读按钮
        self.reread_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 255, 255, 0.10);
                border: 1px solid rgba(255, 255, 255, 0.20);
                border-top: 1px solid rgba(255, 255, 255, 0.36);
                color: rgba(255, 255, 255, 0.92);
                padding: 5px 14px;
                border-radius: 8px;
                font-size: {p['btn_size']}px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: rgba(56, 189, 248, 0.25);
                border-color: rgba(56, 189, 248, 0.6);
                color: #38bdf8;
            }}
            QPushButton:pressed {{
                background: rgba(56, 189, 248, 0.35);
            }}
        """)

        # 关闭按钮
        self.close_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 255, 255, 0.10);
                border: 1px solid rgba(255, 255, 255, 0.20);
                border-top: 1px solid rgba(255, 255, 255, 0.36);
                color: rgba(255, 255, 255, 0.80);
                padding: 5px 14px;
                border-radius: 8px;
                font-size: {p['btn_size']}px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: rgba(239, 68, 68, 0.80);
                border-color: rgba(239, 68, 68, 0.95);
                color: white;
            }}
        """)

        # 学生名字特大号
        self.student_label.setStyleSheet(f"""
            color: #ffffff;
            font-size: {p['name_size']}px;
            font-weight: 800;
            letter-spacing: 0.5px;
            margin-top: 4px;
        """)

        # 事由内容大号
        self.content_label.setStyleSheet(f"""
            color: rgba(255, 255, 255, 0.96);
            font-size: {p['content_size']}px;
            font-weight: 500;
            line-height: 1.45;
            margin-top: 2px;
            margin-bottom: 4px;
        """)

    def enforce_topmost(self):
        try:
            hwnd = int(self.winId())
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                win32con.SWP_SHOWWINDOW | win32con.SWP_NOACTIVATE
            )
        except Exception:
            pass

    def show_notification(self, student, content, timestamp=None, duration_seconds=10, mode="voice"):
        self.current_student = student
        self.current_content = content
        self.current_mode = mode

        if mode == "chime_only":
            self.badge.setText("🔔 黑板小喇叭 · 仅提示音")
            self.reread_btn.setText("🔔 重新提示")
        elif mode == "silent":
            self.badge.setText("🔕 黑板小喇叭 · 静音通知")
            self.reread_btn.setText("📢 语音播报")
        else:
            self.badge.setText("📢 黑板小喇叭 · 教学广播")
            self.reread_btn.setText("🔁 重读一遍")

        self.student_label.setText(f"👤 <span style='color: #38bdf8;'>{student}</span> 同学：")
        self.content_label.setText(content)
        self.time_label.setText(timestamp or time.strftime("%Y-%m-%d %H:%M:%S"))

        self.total_duration_ms = max(4000, duration_seconds * 1000)
        self.remaining_ms = self.total_duration_ms
        self.is_paused = False
        self.pause_hint.setVisible(False)

        # 根据大字号调整卡片尺寸
        p = FONT_PRESETS.get(self.font_preset, FONT_PRESETS["large"])
        screen = QApplication.primaryScreen().geometry()
        card_width = min(p["width"], screen.width() - 60)
        card_height = p["height"]
        self.resize(card_width, card_height)

        target_x = (screen.width() - card_width) // 2
        target_y = 35

        self.move(target_x, -card_height)
        self.show()

        enable_acrylic_blur(int(self.winId()), is_dark=True)
        self.enforce_topmost()

        self.anim = QPropertyAnimation(self, b"pos")
        self.anim.setDuration(420)
        self.anim.setStartValue(QPoint(target_x, -card_height))
        self.anim.setEndValue(QPoint(target_x, target_y))
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim.start()

        self.progress_bar.setValue(100)
        self.tick_timer.start()
        self.topmost_timer.start()

    def on_tick(self):
        if not self.is_paused:
            self.remaining_ms -= 100
            pct = max(0, int((self.remaining_ms / self.total_duration_ms) * 100))
            self.progress_bar.setValue(pct)
            if self.remaining_ms <= 0:
                self.dismiss()

    def enterEvent(self, event):
        self.is_paused = True
        self.pause_hint.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.is_paused = False
        self.pause_hint.setVisible(False)
        super().leaveEvent(event)

    def on_reread_clicked(self):
        mode = getattr(self, 'current_mode', 'voice')
        self.repeat_requested.emit(self.current_student, self.current_content, mode)
        self.remaining_ms = self.total_duration_ms

    def dismiss(self):
        self.tick_timer.stop()
        self.topmost_timer.stop()

        target_y = -self.height() - 20
        self.anim_exit = QPropertyAnimation(self, b"pos")
        self.anim_exit.setDuration(280)
        self.anim_exit.setStartValue(self.pos())
        self.anim_exit.setEndValue(QPoint(self.x(), target_y))
        self.anim_exit.setEasingCurve(QEasingCurve.Type.InCubic)
        self.anim_exit.finished.connect(self.hide)
        self.anim_exit.start()


# ----------------- 消息信号 -----------------
class NotificationSignal(QObject):
    received = pyqtSignal(dict)
    status_changed = pyqtSignal(str)


# ----------------- 二维码生成工具 -----------------
def generate_qr_pixmap(text):
    if not text:
        return None
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=5,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img = img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    qim = QImage(data, img.size[0], img.size[1], QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qim)


# ----------------- 管理员身份验证对话框 -----------------
class AdminPasswordDialog(QDialog):
    def __init__(self, correct_password, parent=None, title="管理员身份验证 - 黑板小喇叭",
                 prompt="进入系统高级设置请输入管理员密码 (初始默认: 888888)", btn_text="验证进入"):
        super().__init__(parent)
        self.correct_password = correct_password
        self.btn_text = btn_text
        self.setWindowTitle(title)
        self.setFixedSize(440, 260)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.setStyleSheet("""
            QDialog {
                background: #ffffff;
                font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header_h = QHBoxLayout()
        icon_lbl = QLabel("🔐")
        icon_lbl.setStyleSheet("font-size: 32px;")
        header_h.addWidget(icon_lbl)

        title_v = QVBoxLayout()
        t1 = QLabel("需要管理员权限")
        t1.setStyleSheet("font-size: 17px; font-weight: bold; color: #0f172a;")
        t2 = QLabel(prompt)
        t2.setStyleSheet("font-size: 12px; color: #64748b;")
        t2.setWordWrap(True)
        title_v.addWidget(t1)
        title_v.addWidget(t2)
        header_h.addLayout(title_v)
        header_h.addStretch()
        layout.addLayout(header_h)

        input_h = QHBoxLayout()
        self.pwd_edit = QLineEdit()
        self.pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pwd_edit.setPlaceholderText("请输入管理员密码...")
        self.pwd_edit.setStyleSheet("""
            QLineEdit {
                background: #f8fafc;
                border: 1.5px solid #cbd5e1;
                border-radius: 8px;
                padding: 8px 12px;
                font-size: 14px;
                color: #0f172a;
            }
            QLineEdit:focus {
                border-color: #0284c7;
                background: #ffffff;
            }
        """)
        input_h.addWidget(self.pwd_edit)

        self.toggle_btn = QPushButton("👁️")
        self.toggle_btn.setToolTip("查看/隐藏密码")
        self.toggle_btn.setFixedSize(38, 38)
        self.toggle_btn.setStyleSheet("""
            QPushButton {
                background: #f1f5f9;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                font-size: 14px;
            }
            QPushButton:hover { background: #e2e8f0; }
        """)
        self.toggle_btn.clicked.connect(self.toggle_echo)
        input_h.addWidget(self.toggle_btn)
        layout.addLayout(input_h)

        self.err_lbl = QLabel("")
        self.err_lbl.setStyleSheet("color: #ef4444; font-size: 12px; font-weight: 600;")
        self.err_lbl.setVisible(False)
        layout.addWidget(self.err_lbl)

        layout.addStretch()

        btn_h = QHBoxLayout()
        btn_h.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setStyleSheet("""
            QPushButton {
                background: #f1f5f9;
                color: #475569;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 8px 18px;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton:hover { background: #e2e8f0; }
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_h.addWidget(cancel_btn)

        confirm_btn = QPushButton(self.btn_text)
        if "退出" in self.btn_text:
            btn_style = """
                QPushButton {
                    background: #dc2626;
                    color: white;
                    border: none;
                    border-radius: 8px;
                    padding: 8px 22px;
                    font-size: 13px;
                    font-weight: 600;
                }
                QPushButton:hover { background: #b91c1c; }
            """
        else:
            btn_style = """
                QPushButton {
                    background: #0284c7;
                    color: white;
                    border: none;
                    border-radius: 8px;
                    padding: 8px 22px;
                    font-size: 13px;
                    font-weight: 600;
                }
                QPushButton:hover { background: #0369a1; }
            """
        confirm_btn.setStyleSheet(btn_style)
        confirm_btn.clicked.connect(self.verify_password)
        btn_h.addWidget(confirm_btn)
        layout.addLayout(btn_h)

        self.pwd_edit.returnPressed.connect(self.verify_password)

    def toggle_echo(self):
        if self.pwd_edit.echoMode() == QLineEdit.EchoMode.Password:
            self.pwd_edit.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self.pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)

    def verify_password(self):
        entered = self.pwd_edit.text().strip()
        if entered == str(self.correct_password):
            self.accept()
        else:
            self.err_lbl.setText("❌ 管理员密码错误，请核对后重试！")
            self.err_lbl.setVisible(True)
            self.pwd_edit.selectAll()
            self.pwd_edit.setFocus()


# ----------------- 系统设置独立对话框 -----------------
class SettingsDialog(QDialog):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.config = main_window.config
        self.setWindowTitle("黑板小喇叭 - 系统高级设置")
        self.setFixedSize(680, 680)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.setStyleSheet("""
            QDialog {
                background: #f8fafc;
                font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
            }
            QTabWidget::pane {
                border: 1px solid #e2e8f0;
                border-radius: 12px;
                background: #ffffff;
                padding: 16px;
            }
            QTabBar::tab {
                background: #f1f5f9;
                color: #64748b;
                padding: 8px 18px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                margin-right: 4px;
                font-size: 13px;
                font-weight: 600;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                color: #0284c7;
                border: 1px solid #e2e8f0;
                border-bottom-color: #ffffff;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title_lbl = QLabel("⚙️ 黑板小喇叭 · 系统高级设置")
        title_lbl.setStyleSheet("font-size: 17px; font-weight: bold; color: #0f172a;")
        layout.addWidget(title_lbl)

        self.tabs = QTabWidget()

        # Tab 1: 班级与信道
        tab_channel = QWidget()
        l_channel = QVBoxLayout(tab_channel)
        l_channel.setSpacing(12)

        # 班级设置组
        cfg_box = QFrame()
        cfg_box.setStyleSheet("background: #f8fafc; border-radius: 10px; border: 1px solid #e2e8f0; padding: 10px;")
        l_cfg = QVBoxLayout(cfg_box)
        l_cfg.setSpacing(8)

        r_h = QHBoxLayout()
        r_h.addWidget(QLabel("🏫 班级编号:"))
        self.room_edit = QLineEdit(self.main_window.room_id)
        self.room_edit.setPlaceholderText("例如 classroom_703")
        self.room_edit.setStyleSheet("background: white; padding: 6px 10px; border: 1px solid #cbd5e1; border-radius: 6px;")
        r_h.addWidget(self.room_edit)
        l_cfg.addLayout(r_h)

        tok_h = QHBoxLayout()
        tok_h.addWidget(QLabel("🔒 教师防伪口令:"))
        self.token_edit = QLineEdit(self.main_window.secret_token)
        self.token_edit.setPlaceholderText("例如 123456 (需与手机端一致)")
        self.token_edit.setStyleSheet("background: white; padding: 6px 10px; border: 1px solid #cbd5e1; border-radius: 6px;")
        tok_h.addWidget(self.token_edit)
        l_cfg.addLayout(tok_h)

        pag_h = QHBoxLayout()
        pag_h.addWidget(QLabel("🌐 Cloudflare Pages 专属域名:"))
        self.pages_edit = QLineEdit(self.main_window.pages_url)
        self.pages_edit.setPlaceholderText("必需配置，例如 https://xxx.pages.dev")
        self.pages_edit.setStyleSheet("background: white; padding: 6px 10px; border: 1px solid #cbd5e1; border-radius: 6px;")
        pag_h.addWidget(self.pages_edit)
        l_cfg.addLayout(pag_h)

        l_channel.addWidget(cfg_box)

        # 二维码与链接区
        qr_box = QFrame()
        qr_box.setStyleSheet("background: #f8fafc; border-radius: 10px; border: 1px solid #e2e8f0; padding: 12px;")
        l_qr = QVBoxLayout(qr_box)
        l_qr.setSpacing(8)

        qr_title = QLabel("📱 手机扫码接入控制台 (即用即发)：")
        qr_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #334155;")
        l_qr.addWidget(qr_title)

        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setMinimumHeight(150)
        l_qr.addWidget(self.qr_label)

        self.url_label = QLabel()
        self.url_label.setOpenExternalLinks(True)
        self.url_label.setWordWrap(True)
        self.url_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l_qr.addWidget(self.url_label)

        self.pages_edit.textChanged.connect(self.update_qr_preview)
        self.room_edit.textChanged.connect(self.update_qr_preview)
        self.token_edit.textChanged.connect(self.update_qr_preview)
        self.update_qr_preview()

        l_channel.addWidget(qr_box)
        l_channel.addStretch()
        self.tabs.addTab(tab_channel, "🏫 班级与信道")

        # Tab 2: 语音与播报
        tab_voice = QWidget()
        l_voice = QVBoxLayout(tab_voice)
        l_voice.setSpacing(12)

        font_h = QHBoxLayout()
        font_h.addWidget(QLabel("🔤 弹窗字体字号:"))
        self.font_combo = QComboBox()
        self.font_combo.setStyleSheet("background: white; padding: 5px 8px; border: 1px solid #cbd5e1; border-radius: 6px;")
        self.font_combo.addItem("标准大字 (34px/26px)", "normal")
        self.font_combo.addItem("教学大屏推荐 (44px/32px)", "large")
        self.font_combo.addItem("远距离特大号 (54px/38px)", "xlarge")
        idx_f = {"normal": 0, "large": 1, "xlarge": 2}.get(self.main_window.font_preset, 1)
        self.font_combo.setCurrentIndex(idx_f)
        font_h.addWidget(self.font_combo)
        l_voice.addLayout(font_h)

        voice_top = QHBoxLayout()
        voice_top.addWidget(QLabel("🗣️ 播报发音人:"))
        self.voice_combo = QComboBox()
        self.voice_combo.setStyleSheet("background: white; padding: 5px 8px; border: 1px solid #cbd5e1; border-radius: 6px;")
        self.populate_voices()
        voice_top.addWidget(self.voice_combo)
        l_voice.addLayout(voice_top)

        rate_h = QHBoxLayout()
        rate_h.addWidget(QLabel("⏩ 播报语速:"))
        self.rate_slider = QSlider(Qt.Orientation.Horizontal)
        self.rate_slider.setRange(-5, 5)
        self.rate_slider.setValue(self.config.get("voice_rate", 0))
        self.rate_slider.valueChanged.connect(lambda v: self.rate_lbl.setText(str(v)))
        rate_h.addWidget(self.rate_slider)
        self.rate_lbl = QLabel(str(self.rate_slider.value()))
        self.rate_lbl.setFixedWidth(24)
        rate_h.addWidget(self.rate_lbl)
        l_voice.addLayout(rate_h)

        vol_h = QHBoxLayout()
        vol_h.addWidget(QLabel("🔊 播报音量:"))
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(self.config.get("voice_volume", 100))
        self.vol_slider.valueChanged.connect(lambda v: self.vol_lbl.setText(f"{v}%"))
        vol_h.addWidget(self.vol_slider)
        self.vol_lbl = QLabel(f"{self.vol_slider.value()}%")
        self.vol_lbl.setFixedWidth(36)
        vol_h.addWidget(self.vol_lbl)
        l_voice.addLayout(vol_h)

        dur_h = QHBoxLayout()
        dur_h.addWidget(QLabel("⏱️ 弹窗停留时长:"))
        self.dur_combo = QComboBox()
        self.dur_combo.setStyleSheet("background: white; padding: 5px 8px; border: 1px solid #cbd5e1; border-radius: 6px;")
        self.dur_combo.addItems(["5 秒", "8 秒", "10 秒", "15 秒", "20 秒"])
        idx_map = {5: 0, 8: 1, 10: 2, 15: 3, 20: 4}
        cur_dur = self.config.get("banner_duration", 10)
        self.dur_combo.setCurrentIndex(idx_map.get(cur_dur, 2))
        dur_h.addWidget(self.dur_combo)
        l_voice.addLayout(dur_h)

        self.chime_cb = QCheckBox("🔔 播报前播放“叮咚”广播提示铃声")
        self.chime_cb.setChecked(self.config.get("play_chime", True))
        l_voice.addWidget(self.chime_cb)

        self.dnd_cb = QCheckBox("🔕 上课免打扰 (仅大屏浮窗，不朗读声音)")
        self.dnd_cb.setChecked(self.config.get("dnd_mode", False))
        l_voice.addWidget(self.dnd_cb)

        l_voice.addStretch()
        self.tabs.addTab(tab_voice, "🗣️ 语音与视效")

        # Tab 3: 系统与安全
        tab_sys = QWidget()
        l_sys = QVBoxLayout(tab_sys)
        l_sys.setSpacing(14)

        self.autostart_cb = QCheckBox("🚀 开机静默启动 (开机自动在后台托盘运行，不弹窗口)")
        self.autostart_cb.setChecked(self.config.get("auto_start", True))
        self.autostart_cb.setStyleSheet("font-size: 13px; font-weight: 500;")
        l_sys.addWidget(self.autostart_cb)

        self.watchdog_cb = QCheckBox("🛡️ 开启双进程自愈防杀看护 (若被任务管理器强杀，0.5秒内自动复活重启)")
        self.watchdog_cb.setChecked(self.config.get("watchdog_enabled", True))
        self.watchdog_cb.setStyleSheet("font-size: 13px; font-weight: bold; color: #0284c7;")
        l_sys.addWidget(self.watchdog_cb)

        # 管理员密码修改框
        pwd_box = QFrame()
        pwd_box.setStyleSheet("background: #f8fafc; border-radius: 10px; border: 1px solid #e2e8f0; padding: 14px;")
        l_pwd = QVBoxLayout(pwd_box)
        l_pwd.setSpacing(8)

        pwd_t = QLabel("🔑 修改管理员设置密码")
        pwd_t.setStyleSheet("font-size: 14px; font-weight: bold; color: #0f172a;")
        l_pwd.addWidget(pwd_t)

        h_old = QHBoxLayout()
        h_old.addWidget(QLabel("原管理员密码:"))
        self.old_pwd_edit = QLineEdit()
        self.old_pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.old_pwd_edit.setStyleSheet("background: white; padding: 5px 8px; border: 1px solid #cbd5e1; border-radius: 6px;")
        h_old.addWidget(self.old_pwd_edit)
        l_pwd.addLayout(h_old)

        h_new1 = QHBoxLayout()
        h_new1.addWidget(QLabel("新管理员密码:"))
        self.new_pwd_edit = QLineEdit()
        self.new_pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.new_pwd_edit.setStyleSheet("background: white; padding: 5px 8px; border: 1px solid #cbd5e1; border-radius: 6px;")
        h_new1.addWidget(self.new_pwd_edit)
        l_pwd.addLayout(h_new1)

        h_new2 = QHBoxLayout()
        h_new2.addWidget(QLabel("确认新密码:"))
        self.confirm_pwd_edit = QLineEdit()
        self.confirm_pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_pwd_edit.setStyleSheet("background: white; padding: 5px 8px; border: 1px solid #cbd5e1; border-radius: 6px;")
        h_new2.addWidget(self.confirm_pwd_edit)
        l_pwd.addLayout(h_new2)

        change_btn = QPushButton("更新管理员密码")
        change_btn.setStyleSheet("""
            QPushButton {
                background: #e0f2fe;
                color: #0369a1;
                border: 1px solid #bae6fd;
                border-radius: 6px;
                padding: 6px 14px;
                font-weight: 600;
            }
            QPushButton:hover { background: #bae6fd; }
        """)
        change_btn.clicked.connect(self.change_admin_password)
        l_pwd.addWidget(change_btn)

        self.pwd_status_lbl = QLabel("")
        self.pwd_status_lbl.setStyleSheet("font-size: 12px; font-weight: 500;")
        self.pwd_status_lbl.setVisible(False)
        l_pwd.addWidget(self.pwd_status_lbl)

        l_sys.addWidget(pwd_box)
        l_sys.addStretch()
        self.tabs.addTab(tab_sys, "🚀 系统与安全")

        layout.addWidget(self.tabs)

        # 底部按钮栏
        bottom_h = QHBoxLayout()
        test_btn = QPushButton("🧪 模拟测试叫号")
        test_btn.setStyleSheet("""
            QPushButton {
                background: #f1f5f9;
                color: #334155;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton:hover { background: #e2e8f0; }
        """)
        test_btn.clicked.connect(self.main_window.local_simulate)
        bottom_h.addWidget(test_btn)

        bottom_h.addStretch()

        cancel_btn = QPushButton("关闭")
        cancel_btn.setStyleSheet("""
            QPushButton {
                background: #f1f5f9;
                color: #64748b;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 8px 18px;
                font-size: 13px;
            }
            QPushButton:hover { background: #e2e8f0; }
        """)
        cancel_btn.clicked.connect(self.reject)
        bottom_h.addWidget(cancel_btn)

        save_btn = QPushButton("💾 保存所有设置")
        save_btn.setStyleSheet("""
            QPushButton {
                background: #0284c7;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 22px;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton:hover { background: #0369a1; }
        """)
        save_btn.clicked.connect(self.save_all_settings)
        bottom_h.addWidget(save_btn)

        layout.addLayout(bottom_h)

    def populate_voices(self):
        try:
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            cnt = speaker.GetVoices().Count
            for i in range(cnt):
                desc = speaker.GetVoices().Item(i).GetDescription()
                self.voice_combo.addItem(desc, i)
            saved_idx = self.config.get("voice_index", 0)
            if 0 <= saved_idx < cnt:
                self.voice_combo.setCurrentIndex(saved_idx)
        except Exception as e:
            print("Populate voices error:", e)

    def change_admin_password(self):
        old_pwd = self.old_pwd_edit.text().strip()
        new_pwd = self.new_pwd_edit.text().strip()
        confirm_pwd = self.confirm_pwd_edit.text().strip()

        cur_pwd = str(self.config.get("admin_password", "888888"))
        if old_pwd != cur_pwd:
            self.pwd_status_lbl.setText("❌ 原管理员密码不正确！")
            self.pwd_status_lbl.setStyleSheet("color: #ef4444; font-size: 12px;")
            self.pwd_status_lbl.setVisible(True)
            return

        if len(new_pwd) < 4:
            self.pwd_status_lbl.setText("❌ 新密码长度至少为 4 位！")
            self.pwd_status_lbl.setStyleSheet("color: #ef4444; font-size: 12px;")
            self.pwd_status_lbl.setVisible(True)
            return

        if new_pwd != confirm_pwd:
            self.pwd_status_lbl.setText("❌ 两次输入的新密码不一致！")
            self.pwd_status_lbl.setStyleSheet("color: #ef4444; font-size: 12px;")
            self.pwd_status_lbl.setVisible(True)
            return

        self.config["admin_password"] = new_pwd
        save_config(self.config)
        self.pwd_status_lbl.setText("✅ 管理员密码修改成功，已生效！")
        self.pwd_status_lbl.setStyleSheet("color: #16a34a; font-size: 12px;")
        self.pwd_status_lbl.setVisible(True)
        self.old_pwd_edit.clear()
        self.new_pwd_edit.clear()
        self.confirm_pwd_edit.clear()

    def update_qr_preview(self):
        pages_url = self.pages_edit.text().strip().rstrip("/")
        room = self.room_edit.text().strip() or "classroom_703"
        token = self.token_edit.text().strip()
        tok_q = f"&token={token}" if token else ""

        if not pages_url:
            self.qr_label.setPixmap(QPixmap())
            self.qr_label.setText("⚠️ 尚未配置 Cloudflare Pages 专属域名\n\n请在上方输入您的 Pages 网址并保存\n例如: https://my-blackboard.pages.dev\n\n手机端将通过该网页在任何网络下直接叫号")
            self.qr_label.setStyleSheet("color: #b45309; font-size: 13px; font-weight: bold; background: #fef3c7; border: 1px dashed #f59e0b; border-radius: 8px; padding: 20px; text-align: center;")
            self.url_label.setText("当前信道状态: 等待绑定 Cloudflare Pages 专属网页")
            self.url_label.setStyleSheet("font-size: 12px; color: #92400e; background: #fffbeb; border: 1px solid #fde68a; padding: 8px 10px; border-radius: 8px;")
        else:
            full_url = f"{pages_url}/?room={room}{tok_q}"
            pix = generate_qr_pixmap(full_url)
            if pix:
                self.qr_label.setText("")
                self.qr_label.setPixmap(pix)
                self.qr_label.setStyleSheet("background: transparent; border: none;")
            self.url_label.setText(f"网址: <a href='{full_url}'>{full_url}</a>")
            self.url_label.setStyleSheet("font-size: 12px; color: #0284c7; background: #f0f9ff; border: 1px solid #bae6fd; padding: 8px 10px; border-radius: 8px;")

    def save_all_settings(self):
        new_room = self.room_edit.text().strip()
        new_token = self.token_edit.text().strip()
        new_pages = self.pages_edit.text().strip().rstrip("/")

        if new_room and new_room != self.main_window.room_id:
            old_room = self.main_window.room_id
            self.main_window.room_id = new_room
            self.config["room_id"] = self.main_window.room_id
            if self.main_window.mqtt_client:
                self.main_window.mqtt_client.unsubscribe(f"school/call/{old_room}")
                self.main_window.mqtt_client.subscribe(f"school/call/{self.main_window.room_id}", qos=1)

        self.main_window.secret_token = new_token
        self.config["secret_token"] = new_token
        self.main_window.pages_url = new_pages
        self.config["pages_url"] = new_pages

        # 语音与字体
        presets = ["normal", "large", "xlarge"]
        chosen_font = presets[self.font_combo.currentIndex()]
        self.config["font_size_preset"] = chosen_font
        self.main_window.font_preset = chosen_font
        self.main_window.banner.set_font_preset(chosen_font)

        self.config["voice_index"] = self.voice_combo.currentIndex()
        self.config["voice_rate"] = self.rate_slider.value()
        self.config["voice_volume"] = self.vol_slider.value()

        dur_map = [5, 8, 10, 15, 20]
        self.config["banner_duration"] = dur_map[self.dur_combo.currentIndex()]
        self.config["play_chime"] = self.chime_cb.isChecked()

        is_dnd = self.dnd_cb.isChecked()
        self.config["dnd_mode"] = is_dnd

        is_autostart = self.autostart_cb.isChecked()
        self.config["auto_start"] = is_autostart
        set_autostart(is_autostart)

        is_watchdog = self.watchdog_cb.isChecked()
        self.config["watchdog_enabled"] = is_watchdog
        if not is_watchdog:
            self.main_window.stop_guardian()
        else:
            self.main_window.ensure_guardian()

        save_config(self.config)
        VoiceService.update_config(self.config)

        self.main_window.refresh_room_info()
        self.main_window.update_dnd_status()

        self.accept()


# ----------------- 主控制台 (只显示通知列表，现代化微质感) -----------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.room_id = self.config.get("room_id", "classroom_703")
        self.pages_url = self.config.get("pages_url", "").rstrip("/")
        self.secret_token = self.config.get("secret_token", "123456")
        self.font_preset = self.config.get("font_size_preset", "large")
        self.item_count = 0

        VoiceService.update_config(self.config)

        tok_q = f"&token={self.secret_token}" if self.secret_token else ""
        self.current_url = f"{self.pages_url}/?room={self.room_id}{tok_q}" if self.pages_url else ""

        self.mqtt_client = None

        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))

        self.signal = NotificationSignal()
        self.signal.received.connect(self.handle_remote_message)
        self.signal.status_changed.connect(self.update_status_ui)

        # 初始化置顶毛玻璃卡片
        self.banner = BannerOverlay(self.font_preset)
        self.banner.repeat_requested.connect(self.manual_re_speak)

        self.setWindowTitle("黑板小喇叭 v3.5")
        self.setMinimumSize(880, 640)
        self.init_ui()
        self.init_tray()

        # 校验并注册开机自启
        if self.config.get("auto_start", True):
            set_autostart(True)

        self.start_mqtt()

        self.is_authorized_quitting = False
        self.guardian_pid = None

        # 双进程自愈守护连接与唤起
        if "--guardian-pid" in sys.argv:
            try:
                idx = sys.argv.index("--guardian-pid")
                self.guardian_pid = int(sys.argv[idx + 1])
                self._start_guardian_watcher_thread()
            except Exception:
                pass
        else:
            self.ensure_guardian()

        if "--recovered" in sys.argv:
            QTimer.singleShot(1200, self._show_recovered_notice)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet("""
            QWidget {
                background: #f8fafc;
                font-family: -apple-system, "Segoe UI Variable Display", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
            }
        """)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(20, 18, 20, 20)
        main_layout.setSpacing(14)

        # ---------------- 顶部操作栏 ----------------
        top_frame = QFrame()
        top_frame.setStyleSheet("""
            QFrame {
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 14px;
                padding: 4px 8px;
            }
        """)
        shadow_top = QGraphicsDropShadowEffect(self)
        shadow_top.setBlurRadius(16)
        shadow_top.setColor(QColor(0, 0, 0, 12))
        shadow_top.setOffset(0, 3)
        top_frame.setGraphicsEffect(shadow_top)

        top_layout = QHBoxLayout(top_frame)
        top_layout.setContentsMargins(14, 10, 14, 10)
        top_layout.setSpacing(12)

        # App Logo & 标题
        logo_lbl = QLabel("📢")
        logo_lbl.setStyleSheet("font-size: 22px; border: none;")
        top_layout.addWidget(logo_lbl)

        title_lbl = QLabel("黑板小喇叭")
        title_lbl.setStyleSheet("font-size: 18px; font-weight: bold; color: #0f172a; border: none;")
        top_layout.addWidget(title_lbl)

        # 班级编号徽章
        self.room_badge = QLabel(f"🏫 班级: {self.room_id}")
        self.room_badge.setStyleSheet("""
            background: #e0f2fe;
            color: #0369a1;
            font-size: 12px;
            font-weight: bold;
            padding: 4px 10px;
            border-radius: 8px;
            border: 1px solid #bae6fd;
        """)
        top_layout.addWidget(self.room_badge)

        # 监听状态徽章
        self.status_indicator = QLabel("🟢 监听中 · MQTT 云端直连")
        self.status_indicator.setStyleSheet("font-size: 13px; font-weight: bold; color: #16a34a; border: none;")
        top_layout.addWidget(self.status_indicator)

        self.dnd_indicator = QLabel("🔕 免打扰")
        self.dnd_indicator.setStyleSheet("""
            background: #fef9c3;
            color: #854d0e;
            font-size: 11px;
            font-weight: bold;
            padding: 3px 8px;
            border-radius: 6px;
            border: 1px solid #fef08a;
        """)
        self.dnd_indicator.setVisible(self.config.get("dnd_mode", False))
        top_layout.addWidget(self.dnd_indicator)

        top_layout.addStretch()

        # 清空列表按钮
        clear_btn = QPushButton("🧹 清空列表")
        clear_btn.setStyleSheet("""
            QPushButton {
                background: #f1f5f9;
                color: #475569;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 6px 12px;
                font-size: 12px;
                font-weight: 500;
            }
            QPushButton:hover { background: #e2e8f0; }
        """)
        clear_btn.clicked.connect(self.clear_history)
        top_layout.addWidget(clear_btn)

        # 系统设置按钮 (带密码锁)
        settings_btn = QPushButton("⚙️ 系统设置 🔒")
        settings_btn.setStyleSheet("""
            QPushButton {
                background: #0284c7;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 6px 14px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton:hover { background: #0369a1; }
        """)
        settings_btn.clicked.connect(self.open_settings_with_auth)
        top_layout.addWidget(settings_btn)

        # 最小化按钮
        min_btn = QPushButton("最小化")
        min_btn.setStyleSheet("""
            QPushButton {
                background: #f1f5f9;
                color: #64748b;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 6px 10px;
                font-size: 12px;
            }
            QPushButton:hover { background: #e2e8f0; }
        """)
        min_btn.clicked.connect(self.hide)
        top_layout.addWidget(min_btn)

        # 退出程序按钮 (需管理员密码)
        exit_btn = QPushButton("退出 🔒")
        exit_btn.setStyleSheet("""
            QPushButton {
                background: #fef2f2;
                color: #dc2626;
                border: 1px solid #fecaca;
                border-radius: 8px;
                padding: 6px 12px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton:hover { background: #fee2e2; }
        """)
        exit_btn.clicked.connect(self.request_quit_with_auth)
        top_layout.addWidget(exit_btn)

        main_layout.addWidget(top_frame)

        # ---------------- 列表标题统计栏 ----------------
        sub_bar = QHBoxLayout()
        self.count_label = QLabel("📋 实时叫号通知列表 (共 0 条)")
        self.count_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #334155; border: none;")
        sub_bar.addWidget(self.count_label)
        sub_bar.addStretch()

        tip_lbl = QLabel("💡 手机扫码或网页发送后，教室大屏自动弹出置顶毛玻璃大字号卡片并朗读")
        tip_lbl.setStyleSheet("font-size: 12px; color: #94a3b8; border: none;")
        sub_bar.addWidget(tip_lbl)
        main_layout.addLayout(sub_bar)

        # ---------------- 叫号通知列表主体 ----------------
        self.history_scroll = QScrollArea()
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setStyleSheet("""
            QScrollArea {
                border: 1px solid #e2e8f0;
                border-radius: 14px;
                background: #ffffff;
            }
        """)

        self.history_container = QWidget()
        self.history_container.setStyleSheet("background: #ffffff;")
        self.history_layout = QVBoxLayout(self.history_container)
        self.history_layout.setContentsMargins(14, 14, 14, 14)
        self.history_layout.setSpacing(12)

        # 空状态占位视图
        self.empty_widget = QWidget()
        empty_layout = QVBoxLayout(self.empty_widget)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.setContentsMargins(0, 80, 0, 80)
        empty_layout.setSpacing(10)

        e_icon = QLabel("📭")
        e_icon.setStyleSheet("font-size: 48px; border: none;")
        e_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(e_icon)

        e_title = QLabel("暂无叫号通知")
        e_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #64748b; border: none;")
        e_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(e_title)

        e_desc = QLabel("教师在操场、走廊使用手机呼叫时，通知记录将实时展示在此处，并带有年-月-日-时-分-秒完整时间。")
        e_desc.setStyleSheet("font-size: 13px; color: #94a3b8; border: none;")
        e_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(e_desc)

        self.history_layout.addWidget(self.empty_widget)
        self.history_layout.addStretch()

        self.history_scroll.setWidget(self.history_container)
        main_layout.addWidget(self.history_scroll)

    def init_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self.tray = QSystemTrayIcon(self)
        if os.path.exists(ICON_PATH):
            self.tray.setIcon(QIcon(ICON_PATH))
        else:
            self.tray.setIcon(self.windowIcon())

        self.tray.setToolTip("黑板小喇叭 (正在后台监听)")

        tray_menu = QMenu()
        show_act = tray_menu.addAction("打开通知列表")
        show_act.triggered.connect(self.show_normal_and_raise)

        settings_act = tray_menu.addAction("⚙️ 系统设置 (需密码)")
        settings_act.triggered.connect(self.open_settings_with_auth)

        test_act = tray_menu.addAction("🧪 模拟测试叫号")
        test_act.triggered.connect(self.local_simulate)

        dnd_act = tray_menu.addAction("切换免打扰模式")
        dnd_act.triggered.connect(self.toggle_dnd_tray)

        tray_menu.addSeparator()
        quit_act = tray_menu.addAction("退出程序 🔒")
        quit_act.triggered.connect(self.request_quit_with_auth)

        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

    def show_normal_and_raise(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_normal_and_raise()

    def open_settings_with_auth(self):
        cur_pwd = self.config.get("admin_password", "888888")
        auth_dlg = AdminPasswordDialog(cur_pwd, self)
        if auth_dlg.exec() == QDialog.DialogCode.Accepted:
            settings_dlg = SettingsDialog(self)
            settings_dlg.exec()

    def request_quit_with_auth(self):
        cur_pwd = self.config.get("admin_password", "888888")
        auth_dlg = AdminPasswordDialog(
            cur_pwd,
            self,
            title="退出程序权限验证 - 黑板小喇叭",
            prompt="退出黑板小喇叭后台服务需要管理员权限，请输入管理员密码：",
            btn_text="验证并退出"
        )
        if auth_dlg.exec() == QDialog.DialogCode.Accepted:
            self.really_quit()

    def refresh_room_info(self):
        self.room_badge.setText(f"🏫 班级: {self.room_id}")
        tok_q = f"&token={self.secret_token}" if self.secret_token else ""
        if self.pages_url:
            self.current_url = f"{self.pages_url}/?room={self.room_id}{tok_q}"
        else:
            self.current_url = ""

    def update_dnd_status(self):
        is_dnd = self.config.get("dnd_mode", False)
        self.dnd_indicator.setVisible(is_dnd)
        if is_dnd:
            self.status_indicator.setText("🔕 免打扰模式 (仅大屏卡片，不朗读)")
            self.status_indicator.setStyleSheet("font-size: 13px; font-weight: bold; color: #eab308; border: none;")
        else:
            self.status_indicator.setText("🟢 监听中 · MQTT 云端直连")
            self.status_indicator.setStyleSheet("font-size: 13px; font-weight: bold; color: #16a34a; border: none;")

    def toggle_dnd_tray(self):
        cur_dnd = self.config.get("dnd_mode", False)
        new_dnd = not cur_dnd
        self.config["dnd_mode"] = new_dnd
        save_config(self.config)
        VoiceService.update_config(self.config)
        self.update_dnd_status()
        self.tray.showMessage(
            "黑板小喇叭",
            f"免打扰模式已{'开启 (仅弹窗不朗读)' if new_dnd else '关闭 (正常语音播报)'}",
            QSystemTrayIcon.MessageIcon.Information,
            2000
        )

    def update_status_ui(self, status):
        self.status_indicator.setText(status)

    def local_simulate(self):
        sample = {
            "room_id": self.room_id,
            "token": self.secret_token,
            "student": "张三",
            "content": "请速到二楼办公室领新学期课本",
            "repeat": 1,
            "mode": "voice",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        self.handle_remote_message(sample)

    def _send_mqtt_response(self, client_id, status, msg):
        if not client_id or not self.mqtt_client:
            return
        try:
            resp_topic = f"school/resp/{client_id}"
            resp_payload = json.dumps({"status": status, "msg": msg})
            self.mqtt_client.publish(resp_topic, resp_payload, qos=1)
        except Exception as e:
            print("MQTT resp publish error:", e)

    def manual_re_speak(self, student, content, mode="voice"):
        if mode == "chime_only":
            VoiceService.play_chime()
        elif mode == "silent":
            pass
        else:
            speech_text = f"请注意，{student}同学，{content}"
            VoiceService.speak(speech_text, repeat=1)

    def handle_remote_message(self, data):
        client_id = data.get("client_id", "")

        # 1. 严格校验班级编号 (双向匹配)
        incoming_room = str(data.get("room_id", "")).strip()
        current_room = str(self.room_id).strip()
        if not incoming_room or incoming_room != current_room:
            err_msg = f"班级编号不匹配！本教室为 [{current_room}]，收到 [{incoming_room}]"
            print(f"[REJECT] {err_msg}")
            self._send_mqtt_response(client_id, status="error", msg=err_msg)
            return

        # 2. 严格校验教师防伪口令 (双向匹配)
        incoming_token = str(data.get("token", "")).strip()
        current_token = str(self.secret_token).strip()
        if incoming_token != current_token:
            err_msg = "教师防伪口令错误，已被教室端安全拒绝！"
            print(f"[REJECT] {err_msg} (收到: '{incoming_token}', 预期: '{current_token}')")
            self._send_mqtt_response(client_id, status="error", msg=err_msg)
            return

        # 3. 校验通过，解析参数并发送成功确认
        student = data.get("student", "同学")
        content = data.get("content", "有新的通知提醒")
        repeat = int(data.get("repeat", 1))
        mode = str(data.get("mode", "voice")).strip().lower()
        if mode not in ["voice", "chime_only", "silent"]:
            mode = "voice"

        mode_name = "叮咚+语音" if mode == "voice" else ("仅叮咚" if mode == "chime_only" else "静音弹窗")
        print(f"[ACCEPT] 收到有效通知: 学生={student}, 模式={mode_name}, 班级={incoming_room}")
        self._send_mqtt_response(client_id, status="ok", msg=f"教室已执行：{student} ({mode_name})")

        # 严格标准化时间为 年-月-日-时-分-秒
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        raw_ts = data.get("timestamp", "")
        if not raw_ts or len(str(raw_ts)) < 19 or "-" not in str(raw_ts):
            timestamp = now_str
        else:
            timestamp = str(raw_ts)

        # 添加到主界面历史记录
        self.add_history_card(student, content, timestamp, repeat, mode=mode)

        # 弹出置顶大屏毛玻璃卡片
        duration = self.config.get("banner_duration", 10)
        self.banner.show_notification(student, content, timestamp, duration, mode=mode)

        # 根据手机端选择的模式调度声音
        is_dnd = self.config.get("dnd_mode", False)
        if not is_dnd:
            if mode == "chime_only":
                # 仅叮咚铃声 + 通知，不朗读
                VoiceService.play_chime()
            elif mode == "silent":
                # 静音通知，无任何声响
                pass
            else:
                # 叮咚铃声 + 语音播报
                speech_text = f"请注意，{student}同学，{content}"
                VoiceService.speak(speech_text, repeat=repeat)

    def add_history_card(self, student, content, timestamp, repeat, mode="voice"):
        self.empty_widget.setVisible(False)
        self.item_count += 1
        self.count_label.setText(f"📋 实时叫号通知列表 (共 {self.item_count} 条)")

        border_color = "#0284c7"
        if mode == "chime_only":
            border_color = "#f59e0b"
        elif mode == "silent":
            border_color = "#94a3b8"

        item_frame = QFrame()
        item_frame.setStyleSheet(f"""
            QFrame {{
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-left: 4px solid {border_color};
                border-radius: 12px;
                padding: 10px 14px;
            }}
            QFrame:hover {{
                border-color: #cbd5e1;
            }}
        """)

        item_layout = QVBoxLayout(item_frame)
        item_layout.setContentsMargins(8, 6, 8, 6)
        item_layout.setSpacing(6)

        # 行 1: 学生姓名 + 提醒模式徽章 + 时间 (年-月-日-时-分-秒)
        row1 = QHBoxLayout()
        name_lbl = QLabel(f"👤 <b>{student}</b> 同学")
        name_lbl.setStyleSheet("color: #0284c7; font-size: 16px; font-weight: bold; border: none;")
        row1.addWidget(name_lbl)

        rpt_badge = QLabel()
        if mode == "chime_only":
            rpt_badge.setText("🔔 仅叮咚铃声")
            rpt_badge.setStyleSheet("font-size: 11px; background: #fef3c7; color: #b45309; padding: 2px 7px; border-radius: 4px; border: 1px solid #fde68a;")
        elif mode == "silent":
            rpt_badge.setText("🔕 静音弹窗")
            rpt_badge.setStyleSheet("font-size: 11px; background: #f1f5f9; color: #64748b; padding: 2px 7px; border-radius: 4px; border: 1px solid #e2e8f0;")
        else:
            rpt_badge.setText(f"📢 语音播报 {repeat} 遍")
            rpt_badge.setStyleSheet("font-size: 11px; background: #f0fdf4; color: #16a34a; padding: 2px 7px; border-radius: 4px; border: 1px solid #bbf7d0;")
        row1.addWidget(rpt_badge)

        row1.addStretch()

        # 时间严格展示: 年-月-日-时-分-秒
        time_lbl = QLabel(f"⏱️ {timestamp}")
        time_lbl.setStyleSheet("color: #64748b; font-size: 13px; font-family: 'Consolas', 'Courier New', monospace; border: none;")
        row1.addWidget(time_lbl)

        item_layout.addLayout(row1)

        # 行 2: 通知内容
        content_lbl = QLabel(f"💬 {content}")
        content_lbl.setWordWrap(True)
        content_lbl.setStyleSheet("color: #1e293b; font-size: 15px; font-weight: 500; border: none; padding: 2px 0;")
        item_layout.addWidget(content_lbl)

        # 行 3: 操作按钮
        row3 = QHBoxLayout()
        row3.addStretch()

        btn_title = "🔁 再次播报" if mode != "chime_only" else "🔔 再次提示"
        re_btn = QPushButton(btn_title)
        re_btn.setStyleSheet("""
            QPushButton {
                background: #f0fdf4;
                color: #16a34a;
                border: 1px solid #bbf7d0;
                padding: 3px 10px;
                border-radius: 6px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #16a34a;
                color: white;
            }
        """)
        re_btn.clicked.connect(lambda: self.manual_re_speak(student, content, mode=mode))
        row3.addWidget(re_btn)

        del_btn = QPushButton("🗑️ 删除")
        del_btn.setStyleSheet("""
            QPushButton {
                background: #fef2f2;
                color: #dc2626;
                border: 1px solid #fecaca;
                padding: 3px 10px;
                border-radius: 6px;
                font-size: 12px;
                font-weight: 500;
            }
            QPushButton:hover {
                background: #dc2626;
                color: white;
            }
        """)
        del_btn.clicked.connect(lambda: self.delete_history_item(item_frame))
        row3.addWidget(del_btn)

        item_layout.addLayout(row3)

        self.history_layout.insertWidget(0, item_frame)

    def delete_history_item(self, item_widget):
        item_widget.setParent(None)
        item_widget.deleteLater()
        self.item_count = max(0, self.item_count - 1)
        self.count_label.setText(f"📋 实时叫号通知列表 (共 {self.item_count} 条)")
        if self.item_count == 0:
            self.empty_widget.setVisible(True)

    def clear_history(self):
        for i in reversed(range(self.history_layout.count())):
            item = self.history_layout.itemAt(i)
            if item and item.widget() and item.widget() != self.empty_widget:
                item.widget().setParent(None)
        self.item_count = 0
        self.count_label.setText("📋 实时叫号通知列表 (共 0 条)")
        self.empty_widget.setVisible(True)

    def start_mqtt(self):
        def on_connect(client, userdata, flags, rc, properties=None):
            if rc == 0:
                topic = f"school/call/{self.room_id}"
                client.subscribe(topic, qos=1)

        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode('utf-8'))
                self.signal.received.emit(payload)
            except Exception as e:
                print("Parse message error:", e)

        def _run_mqtt():
            client_id = f"blackboard_pc_{self.room_id}_{int(time.time())}"
            try:
                self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
            except AttributeError:
                self.mqtt_client = mqtt.Client(client_id=client_id)

            self.mqtt_client.on_connect = on_connect
            self.mqtt_client.on_message = on_message

            try:
                self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
                self.mqtt_client.loop_forever()
            except Exception as e:
                print("MQTT loop error:", e)

        t = threading.Thread(target=_run_mqtt, daemon=True)
        t.start()

    def ensure_guardian(self):
        if not self.config.get("watchdog_enabled", True):
            return
        if getattr(self, 'guardian_pid', None):
            SYNCHRONIZE = 0x00100000
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, self.guardian_pid)
            if h:
                res = ctypes.windll.kernel32.WaitForSingleObject(h, 0)
                ctypes.windll.kernel32.CloseHandle(h)
                if res == 258:  # 守护进程正常活跃中
                    return

        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, "--guardian", str(os.getpid())]
        else:
            cmd = [sys.executable, os.path.abspath(__file__), "--guardian", str(os.getpid())]

        CREATE_NO_WINDOW = 0x08000000
        try:
            p = subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
            self.guardian_pid = p.pid
            self._start_guardian_watcher_thread()
        except Exception as e:
            print("Start guardian error:", e)

    def _start_guardian_watcher_thread(self):
        def _watch():
            SYNCHRONIZE = 0x00100000
            g_pid = getattr(self, 'guardian_pid', None)
            if not g_pid:
                return
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, g_pid)
            if not h:
                return
            ctypes.windll.kernel32.WaitForSingleObject(h, 0xFFFFFFFF)
            ctypes.windll.kernel32.CloseHandle(h)

            # 守护进程被结束：若未处于合法密码退出流程中，立刻拉起新的守护进程进行互相保护
            if not getattr(self, 'is_authorized_quitting', False) and self.config.get("watchdog_enabled", True):
                time.sleep(0.3)
                self.ensure_guardian()

        t = threading.Thread(target=_watch, daemon=True)
        t.start()

    def stop_guardian(self):
        if getattr(self, 'guardian_pid', None):
            try:
                PROCESS_TERMINATE = 0x0001
                h = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, self.guardian_pid)
                if h:
                    ctypes.windll.kernel32.TerminateProcess(h, 0)
                    ctypes.windll.kernel32.CloseHandle(h)
            except Exception:
                pass
            self.guardian_pid = None

    def _show_recovered_notice(self):
        if hasattr(self, 'tray') and self.tray:
            self.tray.showMessage(
                "黑板小喇叭 · 自愈保护",
                "🛡️ 检测到程序曾被任务管理器或异常强杀，自愈守护已在 0.5 秒内自动复活并恢复运行！",
                QSystemTrayIcon.MessageIcon.Warning,
                4000
            )

    def closeEvent(self, event):
        if QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "黑板小喇叭仍在后台运行",
                "现代毛玻璃大字号通知常驻后台，随时响应呼叫。\n如需彻底退出，请点击【退出 🔒】并输入管理员密码。",
                QSystemTrayIcon.MessageIcon.Information,
                2000
            )
        else:
            cur_pwd = self.config.get("admin_password", "888888")
            auth_dlg = AdminPasswordDialog(
                cur_pwd,
                self,
                title="退出程序权限验证 - 黑板小喇叭",
                prompt="退出黑板小喇叭后台服务需要管理员权限，请输入管理员密码：",
                btn_text="验证并退出"
            )
            if auth_dlg.exec() == QDialog.DialogCode.Accepted:
                event.accept()
                self.really_quit()
            else:
                event.ignore()

    def really_quit(self):
        self.is_authorized_quitting = True
        try:
            with open(AUTH_EXIT_FILE, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        except Exception:
            pass
        self.stop_guardian()
        if hasattr(self, 'tray') and self.tray:
            self.tray.hide()
        QApplication.quit()


if __name__ == '__main__':
    # 优先检测是否为后台静默看护守护进程
    if "--guardian" in sys.argv:
        try:
            idx = sys.argv.index("--guardian")
            gui_target_pid = int(sys.argv[idx + 1])
            run_guardian(gui_target_pid)
        except Exception as e:
            print("Guardian error:", e)
        sys.exit(0)

    # 优先获取 Windows 计算机管理员权限
    ensure_admin()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()

    if "--tray" in sys.argv or "-m" in sys.argv:
        window.hide()
    else:
        window.show()

    sys.exit(app.exec())
