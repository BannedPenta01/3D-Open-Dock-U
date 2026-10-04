# vault_mixin.py - tiny JSON-file credential vault.
import os
import json
from PySide6.QtWidgets import QMessageBox
from src.secrets_manager import secure_file


class ManagerVaultMixin:
    def _vault_dir(self):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "3D Open Dock U", "vault")
        os.makedirs(d, exist_ok=True)
        return d

    def refresh_vault_list(self):
        try:
            self.profile_list.clear()
        except Exception:
            return
        d = self._vault_dir()
        for f in sorted(os.listdir(d)):
            if f.endswith(".json"):
                self.profile_list.addItem(os.path.splitext(f)[0])

    def save_to_vault(self):
        name = (self.cemu_username.text().strip() or "profile").replace(" ", "_")
        payload = {
            "username": self.cemu_username.text(),
            "password": self.cemu_password.text(),
            "miiname": self.cemu_miiname.text(),
        }
        path = os.path.join(self._vault_dir(), f"{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        secure_file(path)
        self.refresh_vault_list()
        QMessageBox.information(self, "Vault", f"Saved profile '{name}'.")

    def apply_from_vault(self):
        item = self.profile_list.currentItem() if hasattr(self, "profile_list") else None
        if not item:
            QMessageBox.warning(self, "Vault", "Pick a saved profile first.")
            return
        path = os.path.join(self._vault_dir(), f"{item.text()}.json")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.cemu_username.setText(data.get("username", ""))
            self.cemu_password.setText(data.get("password", ""))
            self.cemu_miiname.setText(data.get("miiname", ""))
            self.save_settings()
            QMessageBox.information(self, "Vault", f"Loaded '{item.text()}'. Now press Patch Cemu.")
        except Exception as e:
            QMessageBox.warning(self, "Vault", str(e))

    def open_vault_folder(self):
        d = self._vault_dir()
        try:
            if os.name == "nt":
                os.startfile(d)
            else:
                QMessageBox.information(self, "Vault", d)
        except Exception as e:
            QMessageBox.warning(self, "Vault", str(e))

    def delete_profile(self):
        item = self.profile_list.currentItem() if hasattr(self, "profile_list") else None
        if not item:
            return
        path = os.path.join(self._vault_dir(), f"{item.text()}.json")
        try:
            os.remove(path)
        except Exception:
            pass
        self.refresh_vault_list()
