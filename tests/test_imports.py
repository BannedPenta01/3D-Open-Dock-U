import os
import sys
import platform
import shutil
import socket
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

try:
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QTabWidget, QLabel, QPushButton, QLineEdit, QTextEdit, QFormLayout,
        QGroupBox, QMessageBox, QFileDialog, QProgressBar, QFrame,
        QScrollArea, QSizePolicy, QSpacerItem, QInputDialog, QDialog,
        QCheckBox, QDialogButtonBox, QScroller, QScrollerProperties,
        QListWidget, QListWidgetItem, QRadioButton, QButtonGroup
    )
    from PySide6.QtCore import Qt, QThread, Signal, QSize, QSettings, QTimer, QDir, QLockFile, QStandardPaths
    from PySide6.QtGui import QColor, QPixmap, QIcon
    print("PySide6 imported successfully")
except ImportError as e:
    print(f"PySide6 import failed: {e}")

try:
    from src import constants
    print("constants imported successfully")
except ImportError as e:
    print(f"constants import failed: {e}")

try:
    from src import utils
    print("utils imported successfully")
except ImportError as e:
    print(f"utils import failed: {e}")

try:
    from src.mixins.server_mixin import ManagerServerMixin
    from src.mixins.vault_mixin import ManagerVaultMixin
    from src.mixins.utils_mixin import ManagerUtilsMixin
    print("mixins imported successfully")
except ImportError as e:
    print(f"mixins import failed: {e}")

try:
    from src import deploy
    print("deploy imported successfully")
except ImportError as e:
    print(f"deploy import failed: {e}")

try:
    from src import patch_emulators
    print("patch_emulators imported successfully")
except ImportError as e:
    print(f"patch_emulators import failed: {e}")

print("All imports finished")
