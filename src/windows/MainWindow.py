import os
import sys
import threading
from PySide6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QLabel, QPushButton
from PySide6.QtGui import QPixmap, QPalette, QPainter, QFontDatabase, QFont, QIcon
from PySide6.QtCore import Qt, QThread, Signal
from ..LutheringLaves import Launcher, LauncherState, ProgressInfo, logger
from ..windows.SettingWindow import SettingsWindow

class DownloadWorker(QThread):
    download_progress = Signal(object)
    verify_progress = Signal(object)
    update_progress = Signal(object)
    
    download_finished = Signal()
    
    # verify_finished = Signal()
    # update_finished = Signal()
    
    error = Signal(str)
    
    def __init__(self, launcher:Launcher):
        super().__init__()
        self.launcher = launcher
        self.launcher.set_progress_callback(self.update_ui_progress)

        self._paused = False
        self._resume_event = threading.Event()
        self._resume_event.set()

        logger.info("DownloadWorker initialized.")
    
    def pause(self):
        self._paused = True
        self._resume_event.clear()
        logger.info("DownloadWorker paused.")
    
    def resume(self):
        self._paused = False
        self._resume_event.set()
        logger.info("DownloadWorker resumed.")
    
    def is_paused(self):
        return self._paused
    
    def update_ui_progress(self, mutil_progress, flag):
        if self._paused:
            self._resume_event.wait()
            
        if flag == "download":
            self.download_progress.emit(mutil_progress[flag])
        elif flag == "verify":
            self.verify_progress.emit(mutil_progress[flag])
        elif flag == "update":
            self.update_progress.emit(mutil_progress[flag])
    
    def run(self):
        try:
            logger.info(f"Worker started with state: {self.launcher.state}")

            if self.launcher.state == LauncherState.NEEDINSTALL:
                logger.info("Starting game download...")
                self.launcher.download_game()
                logger.info("Verifying game files...")
                self.launcher.verify_gamefile()
                self.launcher.state = LauncherState.STARTGAME
                logger.info("Download and verify finished.")
                self.download_finished.emit()
            if self.launcher.state == LauncherState.NEEDUPDATE:
                logger.info("Starting game update...")
                self.launcher.update_game()
                logger.info("Verifying game files after update...")
                self.launcher.verify_gamefile()
                self.launcher.state = LauncherState.STARTGAME
                logger.info("Update and verify finished.")
                self.download_finished.emit()
                
        except Exception as e:
            logger.error(f"Error in DownloadWorker: {e}", exc_info=True)
            self.error.emit(str(e))

class MainWindow(QMainWindow):
    
    def __init__(self):
        super().__init__()
        logger.info("MainWindow initialized.")
        game_folder = "Wuthering Waves Game"
        self.launcher = Launcher(game_folder)
        
        self.setWindowTitle("LutheringLaves")
        self.setFixedSize(1280, 720)
        
        qr = self.frameGeometry()
        cp = self.screen().availableGeometry().center()
        qr.moveCenter(cp)
        self.move(qr.topLeft())
        
        self.set_window_icon()
        
        self.load_custom_font()
        
        # 创建中央部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 创建布局
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        central_widget.setLayout(layout)
        
        # 创建用于显示背景图片的标签
        self.background_label = QLabel()
        self.background_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.background_label)
        
        # 创建设置按钮
        self.settings_button = QPushButton()
        self.settings_button.setFixedSize(60, 55)
        self.settings_button.clicked.connect(self.settings_button_clicked)
        self.settings_button.setStyleSheet("""
            QPushButton {
                background-color: rgba(0, 0, 0, 128);
                border: none;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 130);
            }
            QPushButton:pressed {
                background-color: rgba(255, 255, 255, 160);
            }
        """)
        self.settings_button.setParent(self)
        self.settings_button.setParent(self)
        
        # 设置按钮图标
        icon_path = os.path.join(os.path.dirname(sys.argv[0]), "resource", "setting.svg")
        if os.path.exists(icon_path):
            self.settings_button.setIcon(QIcon(icon_path))
            self.settings_button.setIconSize(self.settings_button.size() * 0.4)
        
        # 创建按钮和日志显示区域
        self.action_button = QPushButton("下载游戏")
        self.action_button.setFixedSize(260, 55)
        self.action_button.clicked.connect(self.action_button_clicked)
        if hasattr(self, 'custom_font'):
            self.action_button.setFont(QFont(self.custom_font, 16, QFont.Bold))
        # 设置按钮样式：白色背景，黑色字体
        self.action_button.setStyleSheet("""
            QPushButton {
                background-color: #e6e6e6;
                color: black;
            }
            QPushButton:hover {
                background-color: #f1f1f1;
            }
            QPushButton:pressed {
                background-color: #e0e0e0;
            }
        """)
        self.action_button.setParent(self)
        
        self.info_label = QLabel("")
        self.info_label.setAlignment(Qt.AlignCenter)
        self.info_label.setFixedSize(260, 30)
        self.info_label.setStyleSheet("""
            QLabel {
                color: white;
                font-size: 12px;
                font-weight: bold;
                background-color: rgba(0, 0, 0, 100);
                border-radius: 5px;
            }
        """)
        self.info_label.setParent(self)
        self.info_label.setVisible(False)
    
        self.set_background_images()
        
        self.init_launcher_state()
        logger.info(f"Launcher state: {self.launcher.state}")
    
    def init_launcher_state(self):
        if self.launcher.state == LauncherState.STARTGAME:
            self.action_button.setText("启动游戏")
            self.action_button.setEnabled(True)
        if self.launcher.state == LauncherState.NEEDINSTALL:
            self.action_button.setText('下载游戏')
            self.action_button.setEnabled(True)
        if self.launcher.state == LauncherState.NEEDUPDATE:
            self.action_button.setText('更新游戏')
            self.action_button.setEnabled(True)

            
    def set_window_icon(self):
        icon_path = os.path.join(os.path.dirname(__file__), "resource", "launcher.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
    def load_custom_font(self):
        font_path = os.path.join(os.path.dirname(sys.argv[0]), "Font", "SourceHanSansCN-VF-2.otf")
        if os.path.exists(font_path):
            font_id = QFontDatabase.addApplicationFont(font_path)
            if font_id != -1:
                font_family = QFontDatabase.applicationFontFamilies(font_id)[0]
                self.custom_font = font_family
            else:
                print("Failed to load custom font")
        else:
            print("Font file not found")
        
    def set_background_images(self):
        
        background_file_name = self.launcher.background_config["background"]
        slogan_file_name = self.launcher.background_config["slogan"]
        # 获取图片路径
        image1_path = os.path.join(os.path.dirname(sys.argv[0]), "resource", slogan_file_name)
        image2_path = os.path.join(os.path.dirname(sys.argv[0]), "resource", background_file_name)

        if os.path.exists(image1_path) and os.path.exists(image2_path):
            # 加载两张图片
            pixmap1 = QPixmap(image1_path)
            pixmap2 = QPixmap(image2_path)
            
            # 创建一个用于组合的 pixmap
            combined_pixmap = QPixmap(self.size())
            combined_pixmap.fill(Qt.transparent)
            
            # 缩放背景图片（WEBP）以适应窗口
            scaled_background = pixmap2.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            
            # 计算背景图片在窗口中的位置（居中）
            bg_x = (self.width() - scaled_background.width()) // 2
            bg_y = (self.height() - scaled_background.height()) // 2
            
            # 缩放前景图片（PNG）以适应窗口大小
            scaled_foreground = pixmap1.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            
            # 计算前景图片在窗口中的位置（居中）
            fg_x = (self.width() - scaled_foreground.width()) // 2
            fg_y = (self.height() - scaled_foreground.height()) // 2
            
            # 将图片绘制到 combined_pixmap 上
            painter = QPainter(combined_pixmap)
            # 先绘制背景图片
            painter.drawPixmap(bg_x, bg_y, scaled_background)
            # 再绘制前景图片（PNG）
            painter.drawPixmap(fg_x, fg_y, scaled_foreground)
            painter.end()
            
            # 设置背景
            self.background_label.setPixmap(combined_pixmap)
        else:
            # 如果没有图片，设置纯色背景
            palette = self.palette()
            palette.setColor(QPalette.Window, Qt.black)
            self.setPalette(palette)
            self.setAutoFillBackground(True)
            
    def resizeEvent(self, event):
        # 窗口大小改变时重新设置背景
        super().resizeEvent(event)
        self.set_background_images()
        
        self.info_label.move((self.width() - self.info_label.width()) - 40,
                            self.height() - self.action_button.height() - self.info_label.height() - 50)
        
        # 更新设置按钮位置到右下角，位于操作按钮左侧
        self.settings_button.move(self.width() - self.action_button.width() - self.settings_button.width() - 50, 
                                 self.height() - self.settings_button.height() - 50)

        # 更新按钮位置到右下角
        self.action_button.move(self.width() - self.action_button.width() - 40, 
                               self.height() - self.action_button.height() - 50)
    
    def settings_button_clicked(self):
        logger.info("Settings button clicked")
        settings_window = SettingsWindow(self, launcher=self.launcher)
        settings_window.exec()

    def action_button_clicked(self):
        logger.info(f"Action button clicked. Current state: {self.launcher.state}")

        if self.launcher.state == LauncherState.STARTGAME:
            self.launcher.start_game_process()
            self.launcher.state = LauncherState.GAMERUNNING
            self.action_button.setEnabled(True)
            self.action_button.setText("停止游戏")
            self.monitor_game_process()
            return

        if self.launcher.state == LauncherState.NEEDINSTALL:
            logger.info("Starting download worker for install.")
            self.worker = DownloadWorker(self.launcher)
            self.worker.download_progress.connect(self.download_progress_ui)
            self.worker.verify_progress.connect(self.verify_progress_ui)
            self.worker.download_finished.connect(self.download_finished_ui)
            self.worker.start()
            return
            
        if self.launcher.state == LauncherState.NEEDUPDATE:
            logger.info("Starting download worker for update.")
            self.worker = DownloadWorker(self.launcher)
            self.worker.update_progress.connect(self.update_progress_ui)
            self.worker.verify_progress.connect(self.verify_progress_ui)
            self.worker.download_finished.connect(self.download_finished_ui)
            self.worker.start()
            return
        
        if self.launcher.state == LauncherState.GAMERUNNING:
            if hasattr(self.launcher, 'game_process') and self.launcher.game_process:
                logger.info("Try to kill game process")
                self.launcher.game_process.kill()
            return
        
        working_state = [LauncherState.DOWNLOADING, LauncherState.UPDATING, LauncherState.VALIDATING]
        if self.launcher.state in working_state:
            if self.worker.is_paused():
                logger.info(f"current state:{self.launcher.state}, resume worker")
                self.worker.resume()
                self.action_button.setText("暂停")
                return
            logger.info(f"current state:{self.launcher.state}, pause worker")
            self.worker.pause()
            self.action_button.setText("继续")
            return

    def download_progress_ui(self, info: ProgressInfo):
        if self.worker.is_paused():
            self.action_button.setText("继续")
            return
        self.action_button.setText("暂停")
        self.info_label.setVisible(True)
        finished_size = info.finished_size
        total_size = info.total_size
        self.info_label.setText(f"已下载 {finished_size / 1024 / 1024 / 1024:.1f}GB / {total_size / 1024 / 1024 /1024:.1f}GB")
    
    def verify_progress_ui(self, info: ProgressInfo):
        if self.worker.is_paused():
            self.action_button.setText("继续")
            return
        self.info_label.setVisible(True)
        finished_size = info.finished_size
        total_size = info.total_size
        self.info_label.setText(f"已校验 {finished_size / 1024 / 1024 / 1024:.1f}GB / {total_size / 1024 / 1024 /1024:.1f}GB")
        
    def update_progress_ui(self, info: ProgressInfo):
        if self.worker.is_paused():
            self.action_button.setText("继续")
            return
        self.action_button.setText("暂停")
        self.info_label.setVisible(True)
        finished_size = info.finished_size
        total_size = info.total_size
        self.info_label.setText(f"已更新 {finished_size / 1024 / 1024 / 1024:.1f}GB / {total_size / 1024 / 1024 /1024:.1f}GB")
    
    def download_finished_ui(self):
        logger.info("Download finished. Ready to launch game.")
        self.action_button.setEnabled(True)
        self.action_button.setText("启动游戏")
        self.info_label.setVisible(False)
    
    def download_error(self, error):
        logger.error(f"Download error: {error}")
        self.action_button.setEnabled(True)
        self.action_button.setText("下载失败")
    
    def monitor_game_process(self):
        if hasattr(self, 'game_process') and self.launcher:
            from PySide6.QtCore import QTimer
            self.process_monitor_timer = QTimer()
            self.process_monitor_timer.timeout.connect(self.check_game_process)
            self.process_monitor_timer.start(1000)  # 每秒检查一次

    def check_game_process(self):
        if hasattr(self.launcher, 'game_process') and self.launcher.game_process:
            # 检查进程是否仍在运行
            if self.launcher.game_process.poll() is not None:
                # 进程已结束
                exit_code = self.launcher.game_process.returncode
                logger.info(f"Game process finished with exit code: {exit_code}")
                
                # 停止定时器
                if hasattr(self, 'process_monitor_timer'):
                    self.process_monitor_timer.stop()
                
                self.on_game_finished(exit_code)
            else:
                # 进程仍在运行
                logger.debug("Game process is still running...")

    def on_game_finished(self, exit_code):
        logger.info(f"Game finished with exit code: {exit_code}")
        self.launcher.state = LauncherState.STARTGAME
        self.action_button.setEnabled(True)
        self.action_button.setText("启动游戏")