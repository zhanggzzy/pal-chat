from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from pal_chat_server.config import Settings


def _load_keyring() -> Any | None:
    try:
        import keyring
    except ImportError:  # pragma: no cover
        return None
    return keyring


def _keyring_backend_available(keyring_module: Any) -> bool:
    backend = keyring_module.get_keyring()
    priority = getattr(backend, "priority", 0)
    return bool(priority and priority > 0)


class SecretStore(Protocol):
    def set_secret(self, credential_ref: str, secret: str) -> None: ...

    def get_secret(self, credential_ref: str) -> str | None: ...

    def delete_secret(self, credential_ref: str) -> None: ...


@dataclass(slots=True)
class MemorySecretStore:
    secrets: dict[str, str] = field(default_factory=dict)

    def set_secret(self, credential_ref: str, secret: str) -> None:
        self.secrets[credential_ref] = secret

    def get_secret(self, credential_ref: str) -> str | None:
        return self.secrets.get(credential_ref)

    def delete_secret(self, credential_ref: str) -> None:
        self.secrets.pop(credential_ref, None)


@dataclass(slots=True)
class FileSecretStore:
    path: Path

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return cast(dict[str, str], json.loads(self.path.read_text(encoding="utf-8")))

    def _write(self, secrets: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(secrets), encoding="utf-8")

    def set_secret(self, credential_ref: str, secret: str) -> None:
        secrets = self._read()
        secrets[credential_ref] = secret
        self._write(secrets)

    def get_secret(self, credential_ref: str) -> str | None:
        return self._read().get(credential_ref)

    def delete_secret(self, credential_ref: str) -> None:
        secrets = self._read()
        if credential_ref not in secrets:
            return
        del secrets[credential_ref]
        self._write(secrets)


@dataclass(slots=True)
class KeyringSecretStore:
    service_name: str
    fallback_store: SecretStore | None = None

    def set_secret(self, credential_ref: str, secret: str) -> None:
        keyring_module = _load_keyring()
        if keyring_module is None or not _keyring_backend_available(keyring_module):
            if self.fallback_store is None:  # pragma: no cover
                raise RuntimeError("keyring backend is unavailable")
            self.fallback_store.set_secret(credential_ref, secret)
            return
        try:
            keyring_module.set_password(self.service_name, credential_ref, secret)
        except Exception:
            if self.fallback_store is None:
                raise
            self.fallback_store.set_secret(credential_ref, secret)

    def get_secret(self, credential_ref: str) -> str | None:
        keyring_module = _load_keyring()
        if keyring_module is None or not _keyring_backend_available(keyring_module):
            if self.fallback_store is None:
                return None
            return self.fallback_store.get_secret(credential_ref)
        try:
            secret = cast(
                str | None,
                keyring_module.get_password(self.service_name, credential_ref),
            )
        except Exception:
            if self.fallback_store is None:
                raise
            return self.fallback_store.get_secret(credential_ref)
        if secret is not None or self.fallback_store is None:
            return secret
        return self.fallback_store.get_secret(credential_ref)

    def delete_secret(self, credential_ref: str) -> None:
        keyring_module = _load_keyring()
        if keyring_module is None or not _keyring_backend_available(keyring_module):
            if self.fallback_store is not None:
                self.fallback_store.delete_secret(credential_ref)
            return
        try:
            keyring_module.delete_password(self.service_name, credential_ref)
        except Exception:
            if self.fallback_store is None:
                return
        if self.fallback_store is not None:
            self.fallback_store.delete_secret(credential_ref)


_MEMORY_STORE = MemorySecretStore()


def secret_store_from_settings(settings: Settings) -> SecretStore:
    if settings.credential_backend == "memory":
        return _MEMORY_STORE
    file_store = FileSecretStore(settings.data_dir / "credential-secrets.json")
    if settings.credential_backend == "file":
        return file_store
    return KeyringSecretStore(settings.keyring_service_name, fallback_store=file_store)


def mask_secret(secret: str) -> str:
    tail = secret[-4:] if len(secret) >= 4 else secret
    return f"***{tail}"
