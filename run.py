import sys
from PyQt6.QtWidgets import QApplication

from main_window import MainWindow
from config import load_config

if __name__ == "__main__":
    # Load the configuration first
    load_config()

    # Create and run the PyQt application
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())