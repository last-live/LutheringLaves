import sys
import os
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from .LutheringLaves import logger
from .windows.MainWindow import MainWindow


def run():
    logger.info("Application started.")
    app = QApplication(sys.argv)
    
    icon_path = os.path.join(os.path.dirname(sys.argv[0]), "resource", "launcher.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())