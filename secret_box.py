from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet


@dataclass(slots=True)
class SecretBox:
    key_path: Path
    persist_to_disk: bool = True
    _fernet: Fernet | None = None

    def __post_init__(self) -> None:
        if self.persist_to_disk:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.key_path.exists():
                self.key_path.write_bytes(Fernet.generate_key())
            key = self.key_path.read_bytes()
        else:
            key = Fernet.generate_key()
        self._fernet = Fernet(key)

    def encrypt(self, text: str) -> str:
        return self._fernet.encrypt(text.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")

    def encrypt_json(self, payload: Any) -> str:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        compressed = zlib.compress(raw, level=9)
        return self._fernet.encrypt(compressed).decode("utf-8")

    def decrypt_json(self, token: str) -> Any:
        compressed = self._fernet.decrypt(token.encode("utf-8"))
        raw = zlib.decompress(compressed)
        return json.loads(raw.decode("utf-8"))
