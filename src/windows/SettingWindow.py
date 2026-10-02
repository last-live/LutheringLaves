from PySide6.QtWidgets import QVBoxLayout, QDialog, QComboBox, QWidget, QCheckBox, QGroupBox, QFormLayout, QPushButton, QHBoxLayout, QLabel, QMessageBox
from PySide6.QtCore import Qt
from src.LutheringLaves import Launcher, logger, QUALITY_LEVELS, QUALITY_NAMES

class SettingsWindow(QDialog):
    def __init__(self, parent=None, launcher: Launcher = None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setFixedSize(600, 520)
        
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
        
        # 添加资源档位分组
        self.add_quality_group(layout)
        
    def add_quality_group(self, layout):
        # 资源档位
        quality_group = QGroupBox("资源档位")
        quality_layout = QFormLayout()
        quality_group.setLayout(quality_layout)
        self.quality_status = self.launcher.get_quality_status()
        self.quality_combo = QComboBox()
        self.populate_quality_combo()
        self.quality_combo.currentIndexChanged.connect(self.on_quality_changed)
        quality_layout.addRow("下载画质：", self.quality_combo)
        self.quality_tip_label = QLabel()
        self.quality_tip_label.setWordWrap(True)
        quality_layout.addRow("", self.quality_tip_label)
        self.release_button = QPushButton("释放其它档位资源")
        self.release_button.setFixedSize(160, 28)
        self.release_button.clicked.connect(self.on_release_quality_clicked)
        quality_layout.addRow("", self.release_button)
        layout.addWidget(quality_group)
        self.refresh_quality_group()
    
    def populate_quality_combo(self):
        self.quality_combo.blockSignals(True)
        self.quality_combo.clear()
        current_quality = self.launcher.get_download_quality()
        current_index = 0
        if self.quality_status:
            for i, item in enumerate(self.quality_status):
                size_text = f"{item['size'] / 1024 ** 3:.1f} GiB" if item['size'] else "大小未知"
                state_text = "已下载" if item['downloaded'] else "未下载"
                self.quality_combo.addItem(f"{item['name']}（{size_text}，{state_text}）", item['key'])
                if item['key'] == current_quality:
                    current_index = i
        else:
            for i, quality in enumerate(QUALITY_LEVELS):
                self.quality_combo.addItem(QUALITY_NAMES.get(quality, quality), quality)
                if quality == current_quality:
                    current_index = i
        self.quality_combo.setCurrentIndex(current_index)
        self.quality_combo.blockSignals(False)
    
    def refresh_quality_group(self):
        quality = self.launcher.get_download_quality()
        status = self.quality_status
        if not status:
            self.quality_tip_label.setText("无法获取官方资源包信息（网络异常），档位资源将在下载/更新时按官方索引获取。")
            self.release_button.setEnabled(False)
            return
        active = next((item for item in status if item['key'] == quality), None)
        others = [item for item in status if item['key'] != quality and item['downloaded']]
        if active and active['downloaded']:
            self.quality_tip_label.setText(f"当前使用：{active['name']}，资源已就绪。")
        elif active:
            size_text = f"（约 {active['size'] / 1024 ** 3:.1f} GiB）" if active['size'] else ""
            self.quality_tip_label.setText(
                f"当前选择的 {active['name']} 资源尚未下载{size_text}，"
                f"请回到主界面点击「更新游戏」下载；已下载的档位也可直接切换使用。"
            )
        else:
            self.quality_tip_label.setText("档位资源将在下载/更新时按官方索引获取。")
        if others:
            names = "、".join(item['name'] for item in others)
            self.release_button.setEnabled(True)
            self.release_button.setToolTip(f"删除已下载的其它档位资源（{names}）以释放磁盘空间")
        else:
            self.release_button.setEnabled(False)
            self.release_button.setToolTip("没有已下载的其它档位资源")
    
    def on_quality_changed(self, index):
        quality = self.quality_combo.currentData()
        if not quality:
            return
        logger.info(f"下载画质变更为: {quality}")
        self.launcher.settings['download_quality'] = quality
        self.launcher.update_settings()
        self.refresh_quality_group()
    
    def on_release_quality_clicked(self):
        quality = self.launcher.get_download_quality()
        status = self.launcher.get_quality_status() or []
        others = [item for item in status if item['key'] != quality and item['downloaded']]
        if not others:
            QMessageBox.information(self, "释放空间", "没有已下载的其它档位资源。")
            return
        detail = "\n".join(
            f"· {item['name']}（{item['size'] / 1024 ** 3:.1f} GiB）" if item['size'] else f"· {item['name']}"
            for item in others
        )
        answer = QMessageBox.question(
            self, "释放空间",
            f"将删除以下已下载档位资源以释放磁盘空间：\n{detail}\n\n删除后如需再次使用这些档位，需要重新下载。是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if answer != QMessageBox.Yes:
            return
        try:
            deleted, freed = self.launcher.release_quality_packs([item['key'] for item in others])
        except Exception as e:
            QMessageBox.warning(self, "释放空间", f"释放失败：{e}")
            return
        logger.info(f"已释放 {deleted} 个文件，约 {freed / 1024 ** 3:.1f} GiB")
        QMessageBox.information(self, "释放空间", f"已删除 {deleted} 个文件，释放约 {freed / 1024 ** 3:.1f} GiB 空间。")
        self.quality_status = self.launcher.get_quality_status()
        self.populate_quality_combo()
        self.refresh_quality_group()
    
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