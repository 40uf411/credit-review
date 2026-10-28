"""Where the service keeps its files: a local folder, or Azure Blob Storage in the cloud.

Keys are slash-separated paths (`llm/ab/<key>.json`, `<dossier id>/status.json`). The same keys work on
both backends, so a cache filled locally has the same layout as the one in Blob.

    CREDIT_REVIEW_STORAGE_URL=https://<account>.blob.core.windows.net   → Blob, signed in with Entra ID
    (unset)                                                             → a local folder

Blob access uses DefaultAzureCredential: the app's managed identity in Azure (AZURE_CLIENT_ID picks the
user-assigned one), `az login` on a laptop. The storage account has shared keys disabled, so there is no
connection string to leak.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Protocol


class Store(Protocol):
    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, data: bytes) -> None: ...
    def list(self, prefix: str) -> list[str]: ...


def _safe(key: str) -> str:
    parts = key.split("/")
    if not key or any(p in ("", ".", "..") for p in parts) or "\\" in key:
        raise ValueError(f"invalid storage key {key!r}")
    return key


class LocalStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def __repr__(self) -> str:
        return f"LocalStore({str(self.root)!r})"

    def get(self, key: str) -> bytes | None:
        path = self.root / _safe(key)
        return path.read_bytes() if path.is_file() else None

    def put(self, key: str, data: bytes) -> None:
        path = self.root / _safe(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)         # readers never see a half-written file

    def list(self, prefix: str) -> list[str]:
        base = self.root / _safe(prefix.rstrip("/")) if prefix.strip("/") else self.root
        if not base.is_dir():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*")
                      if p.is_file() and not p.name.endswith(".tmp"))


class BlobStore:
    def __init__(self, account_url: str, container: str, container_client: Any = None):
        self.account_url, self.container = account_url.rstrip("/"), container
        if container_client is None:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import ContainerClient
            container_client = ContainerClient(self.account_url, container, credential=DefaultAzureCredential())
        self.client = container_client

    def __repr__(self) -> str:
        return f"BlobStore({self.account_url!r}, {self.container!r})"

    def get(self, key: str) -> bytes | None:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            return self.client.download_blob(_safe(key)).readall()
        except ResourceNotFoundError:
            return None

    def put(self, key: str, data: bytes) -> None:
        self.client.upload_blob(_safe(key), data, overwrite=True)

    def list(self, prefix: str) -> list[str]:
        return sorted(b.name for b in self.client.list_blobs(name_starts_with=prefix))


def store_from_env(container: str, local_root: str | Path) -> Store:
    """Blob when CREDIT_REVIEW_STORAGE_URL is set, otherwise a local folder."""
    url = os.environ.get("CREDIT_REVIEW_STORAGE_URL", "").strip()
    return BlobStore(url, container) if url else LocalStore(local_root)
