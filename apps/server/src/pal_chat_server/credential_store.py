from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from pal_chat_server.config import Settings


def _load_keyring() -> Any | None:
    try:
        import keyring
    except ImportError:  # pragma: no cover
        return None
    return keyring


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
class KeyringSecretStore:
    service_name: str

    def set_secret(self, credential_ref: str, secret: str) -> None:
        keyring_module = _load_keyring()
        if keyring_module is None:  # pragma: no cover
            raise RuntimeError("keyring is not installed")
        keyring_module.set_password(self.service_name, credential_ref, secret)

    def get_secret(self, credential_ref: str) -> str | None:
        keyring_module = _load_keyring()
        if keyring_module is None:  # pragma: no cover
            return None
        return cast(
            str | None,
            keyring_module.get_password(self.service_name, credential_ref),
        )

    def delete_secret(self, credential_ref: str) -> None:
        keyring_module = _load_keyring()
        if keyring_module is None:  # pragma: no cover
            return
        try:
            keyring_module.delete_password(self.service_name, credential_ref)
        except Exception:
            return


_MEMORY_STORE = MemorySecretStore()


def secret_store_from_settings(settings: Settings) -> SecretStore:
    if settings.credential_backend == "memory":
        return _MEMORY_STORE
    return KeyringSecretStore(settings.keyring_service_name)


def mask_secret(secret: str) -> str:
    tail = secret[-4:] if len(secret) >= 4 else secret
    return f"***{tail}"
