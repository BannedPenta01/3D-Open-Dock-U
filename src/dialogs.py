# dialogs.py
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QLineEdit, 
                               QPushButton, QHBoxLayout, QMessageBox, QApplication,
                               QTextEdit, QCheckBox, QDialogButtonBox)
from PySide6.QtCore import Qt
from datetime import datetime
from src.constants import STYLESHEET, RED_LIGHT, BG_INPUT, CYAN_LIGHT, TEXT_SECONDARY, BG_CARD_HOVER, CYAN_PRIMARY, RED_DARK

class ErrorPopupDialog(QDialog):
    def __init__(self, parent, error_msg):
        super().__init__(parent)
        self.setWindowTitle("Critical Server Error Detected")
        self.setMinimumWidth(650)
        self.setStyleSheet(STYLESHEET)
        
        layout = QVBoxLayout(self)
        
        header = QHBoxLayout()
        icon = QLabel("⚠️")
        icon.setStyleSheet("font-size: 32px;")
        header.addWidget(icon)
        
        title_text = QLabel("A Problematic Server Log was Detected")
        title_text.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {RED_LIGHT};")
        header.addWidget(title_text, 1)
        layout.addLayout(header)
        
        desc = QLabel("The following error was captured from the server logs while the session was active.")
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {TEXT_SECONDARY}; margin-bottom: 10px;")
        layout.addWidget(desc)
        
        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setPlainText(error_msg)
        self.text_edit.setStyleSheet(f"background: {BG_INPUT}; color: {CYAN_LIGHT}; font-family: monospace; border: 1px solid {RED_DARK};")
        self.text_edit.setMinimumHeight(150)
        layout.addWidget(self.text_edit)
        
        btn_layout = QHBoxLayout()
        copy_btn = QPushButton("Copy Report")
        copy_btn.setStyleSheet(f"background: {BG_CARD_HOVER}; border-color: {CYAN_PRIMARY};")
        copy_btn.clicked.connect(self.copy_to_clip)
        btn_layout.addWidget(copy_btn)
        
        close_btn = QPushButton("Dismiss")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        
        layout.addLayout(btn_layout)
        
    def copy_to_clip(self):
        full_text = f"--- 3D Open Dock U CRITICAL ERROR REPORT ---\nTimestamp: {datetime.now().isoformat()}\nDetected Log Line:\n{self.text_edit.toPlainText()}\n--------------------------------------------"
        QApplication.clipboard().setText(full_text)
        QMessageBox.information(self, "Copied", "Error report copied to clipboard.")

class SudoPasswordDialog(QDialog):
    def __init__(self, parent=None, current_pw=""):
        super().__init__(parent)
        self.setWindowTitle("Administrator Password")
        self.setMinimumWidth(360)
        self.setStyleSheet(STYLESHEET)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Sudo Access Required", styleSheet="font-size: 18px; font-weight: bold;"))
        layout.addWidget(QLabel("Enter your Linux password:"))
        
        pass_row = QHBoxLayout()
        self.pass_field = QLineEdit(current_pw)
        self.pass_field.setEchoMode(QLineEdit.Password)
        pass_row.addWidget(self.pass_field)
        
        self.eye_btn = QPushButton("👁")
        self.eye_btn.setCheckable(True)
        self.eye_btn.setFixedSize(32, 32)
        self.eye_btn.setStyleSheet("font-size: 18px; padding: 0;")
        self.eye_btn.clicked.connect(self.toggle_visibility)
        pass_row.addWidget(self.eye_btn)
        layout.addLayout(pass_row)

        self.remember_cb = QCheckBox("Remember for this session")
        self.remember_cb.setStyleSheet("color: #8888AA;")
        self.remember_cb.setChecked(True)
        layout.addWidget(self.remember_cb)
        
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        
    def toggle_visibility(self):
        if self.eye_btn.isChecked():
            self.pass_field.setEchoMode(QLineEdit.Normal)
        else:
            self.pass_field.setEchoMode(QLineEdit.Password)
            
    def get_data(self):
        return self.pass_field.text(), self.remember_cb.isChecked()
