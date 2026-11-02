"""Throwaway file keyring, selected explicitly in every child process."""

import json
import os
from pathlib import Path

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class FileKeyring(KeyringBackend):
    priority = 100

    def _path(self):
        return Path(os.environ["HOME"]) / "blackbox-keyring.json"

    def _load(self):
        path = self._path()
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _save(self, data):
        self._path().write_text(json.dumps(data), encoding="utf-8")

    def get_password(self, service, username):
        return self._load().get(service, {}).get(username)

    def set_password(self, service, username, password):
        data = self._load()
        data.setdefault(service, {})[username] = password
        self._save(data)

    def delete_password(self, service, username):
        data = self._load()
        try:
            del data[service][username]
        except KeyError as exc:
            raise PasswordDeleteError(username) from exc
        self._save(data)
