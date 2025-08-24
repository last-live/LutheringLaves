from PySide6.QtWidgets import QVBoxLayout, QLabel, QDialog
from PySide6.QtCore import Qt

class SettingsWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setFixedSize(600, 400)
        
        # 居中显示在主窗口上
        if parent:
            parent_geo = parent.geometry()
            self.move(
                parent_geo.center().x() - self.width() // 2,
                parent_geo.center().y() - self.height() // 2 - 50
            )
        
        # 添加设置界面内容
        layout = QVBoxLayout()
        self.setLayout(layout)
        
        label = QLabel("设置窗口")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)