from PySide6.QtWidgets import QVBoxLayout, QDialog, QComboBox, QWidget, QCheckBox, QGroupBox, QFormLayout, QPushButton, QHBoxLayout
from PySide6.QtCore import Qt
from src.LutheringLaves import Launcher, logger

class SettingsWindow(QDialog):
    def __init__(self, parent=None, launcher: Launcher = None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setFixedSize(600, 400)
        
        self.launcher = launcher
        
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
        
        # 添加下拉框分组
        self.add_combo_group(layout)
        
        # 添加复选框分组
        self.add_checkbox_group(layout)
        
        # 添加按钮
        #self.add_button(layout)
        
        # 添加弹性空间
        layout.addStretch()
        
        # 添加底部按钮
        self.add_bottom_buttons(layout)
    
    def add_combo_group(self, layout):
        # 创建下拉框分组
        combo_group = QGroupBox("Proton 版本选择")
        combo_layout = QFormLayout()
        combo_group.setLayout(combo_layout)
        
        current_proton_path = self.launcher.settings.get('proton_path', '')
        
        geproton_versions, proton_versions = self.launcher.find_available_proton()
        available_protons = geproton_versions + proton_versions

        self.combo_box = QComboBox()
        
        if len(available_protons) > 0:
            # 创建下拉框
            for i in range(len(available_protons)):
                proton = available_protons[i]
                self.combo_box.addItem(proton['version'], proton['proton_path'])
                if proton['proton_path'] == current_proton_path:
                    self.combo_box.setCurrentIndex(i)
        else:
            self.combo_box.addItem("无可用Proton版本")

        # 连接下拉框变化信号
        self.combo_box.currentTextChanged.connect(self.on_combo_changed)

        # 添加到表单布局
        combo_layout.addRow("Proton 版本：", self.combo_box)
        
        layout.addWidget(combo_group)
        
    def add_checkbox_group(self, layout):
        # 创建复选框分组
        checkbox_group = QGroupBox("启动参数")
        checkbox_layout = QVBoxLayout()
        checkbox_group.setLayout(checkbox_layout)
        
        # 定义所有复选框的配置信息
        checkbox_configs = [
            {
                "var": "steamappid",
                "text": "STEAMAPPID=3513350 模拟为Steam库鸣潮，勾选后首次启动等待时间较长",
                "checked": self.launcher.settings.get('steamappid', '0') == "3513350"
            },
            {
                "var": "proton_media_use_gst",
                "text": "PROTON_MEDIA_USE_GST=1 游戏内视频异常时勾选",
                "checked": self.launcher.settings.get('proton_media_use_gst', '0') == "1"
            },
            {
                "var": "proton_enable_wayland",
                "text": "PROTON_ENABLE_WAYLAND=1 WAYLAND适配",
                "checked": self.launcher.settings.get('proton_enable_wayland', '0') == "1"
            },
            {
                "var": "proton_no_d3d12",
                "text": "PROTON_NO_D3D12=1 关闭DX12，游戏以DX11运行",
                "checked": self.launcher.settings.get('proton_no_d3d12', '0') == "1"
            },
            {
                "var": "mangohud",
                "text": "MANGOHUD=1 显示游戏帧数",
                "checked": self.launcher.settings.get('mangohud', '0') == "1"
            }
        ]
        
        # 动态创建复选框并连接信号
        for config in checkbox_configs:
            checkbox = QCheckBox(config["text"])
            checkbox.setChecked(config["checked"])
            
            # 使用lambda表达式捕获复选框对象名称
            checkbox_name = config["var"]
            checkbox.stateChanged.connect(
                lambda state, name=checkbox_name: self.on_checkbox_changed(name, state)
            )
            

            checkbox_layout.addWidget(checkbox)
            setattr(self, config["var"], checkbox)
        
        layout.addWidget(checkbox_group)
    
    def add_bottom_buttons(self, layout):
        # 创建底部按钮布局
        button_layout = QHBoxLayout()
        
        # 添加弹性空间，使按钮靠右对齐
        button_layout.addStretch()
        
        # 创建关闭按钮
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.accept)
        self.close_button.setFixedSize(100, 30)
        
        # 添加按钮到布局
        button_layout.addWidget(self.close_button)
        
        layout.addLayout(button_layout)
        
    def add_button(self, layout):
        # 创建按钮容器
        button_widget = QWidget()
        button_layout = QVBoxLayout()
        button_widget.setLayout(button_layout)
        
        self.reset_button = QPushButton("修复游戏文件")
        self.reset_button.clicked.connect(self.on_fix_button_clicked)
        self.reset_button.setFixedSize(150, 30)
        
        self.save_button = QPushButton("清理compatdata")
        self.save_button.clicked.connect(self.on_clear_button_clicked)
        self.save_button.setFixedSize(150, 30)
        
        # 添加按钮到布局
        button_layout.addWidget(self.reset_button)
        button_layout.addWidget(self.save_button)
        
        layout.addWidget(button_widget)
    
    def on_combo_changed(self, text):
        # 下拉框选项变化时的处理函数
        current_data = self.combo_box.currentData()
        if current_data:
            self.combo_box.currentIndex()
            self.launcher.settings['proton_path'] = current_data
            self.launcher.settings['proton_version'] = text
            self.launcher.update_settings()

    def on_checkbox_changed(self, checkbox_name, state):
        # 复选框状态变化时的处理函数
        checkbox = getattr(self, checkbox_name, None)
        
        if checkbox:
            is_checked = state == 2  # 2表示选中，0表示未选中
            logger.info(f"{checkbox_name} 状态改变为: {'选中' if is_checked else '未选中'}")
            if checkbox_name == "steamappid":
                self.launcher.settings['steamappid'] = "3513350" if is_checked else "0"
            elif checkbox_name == "proton_media_use_gst":
                self.launcher.settings['proton_media_use_gst'] = "1" if is_checked else "0"
            elif checkbox_name == "proton_enable_wayland":
                self.launcher.settings['proton_enable_wayland'] = "1" if is_checked else "0"
            elif checkbox_name == "proton_no_d3d12":
                self.launcher.settings['proton_no_d3d12'] = "1" if is_checked else "0"
            elif checkbox_name == "mangohud":
                self.launcher.settings['mangohud'] = "1" if is_checked else "0"
                
            self.launcher.update_settings()


    def on_fix_button_clicked(self):
        pass
        
    def on_clear_button_clicked(self):
        pass