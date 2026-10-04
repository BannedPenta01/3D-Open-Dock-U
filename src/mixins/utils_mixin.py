# utils_mixin.py - restore / reset helpers.
from PySide6.QtWidgets import QMessageBox


class ManagerUtilsMixin:
    def restore_nintendo_official(self):
        self.mode_pretendo.setChecked(False)
        self.mode_local.setChecked(True)
        self.patcher.patch_cemu_settings("https://api.accounts.nintendo.com", True)
        self.patcher.patch_citra("nintendo_restore")
        QMessageBox.information(self, "Restored", "Emulators point back to Nintendo.")

    def restore_pretendo_official(self):
        try:
            self.mode_pretendo.setChecked(True)
        except Exception:
            pass
        self.patcher.patch_cemu_settings("https://api.pretendo.network", True)
        self.patcher.patch_citra("official_restore")
        QMessageBox.information(self, "Restored", "Emulators point to Pretendo Network.")

    def reset_to_defaults(self):
        if QMessageBox.question(self, "Factory Reset",
                "Reset emulator network files to defaults?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.restore_pretendo_official()
