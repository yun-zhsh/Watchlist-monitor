"""Keep the SQLite database in a Google Cloud Storage object, for the Cloud Run job and web page.

A SQLite file cannot live on a mounted bucket (it needs file locking), so each process works on a copy on its own disk:

  update job   pull the object  ->  update the local copy  ->  push it back, only if the object is still the version that was pulled
  web page     pull the object when its version changed (checked every few minutes); it never uploads

Settings (environment variables):
  VALUATION_DB_URI       gs://bucket/valuation.db  (the object; without it nothing here is used and the database is just a local file)
  VALUATION_DB_PATH      where the local copy lives (the data directory's valuation.db by default)
  VALUATION_UPDATE_JOB   projects/PROJECT/locations/REGION/jobs/NAME: the Cloud Run job the "Update data" button starts

Next to the database, the update job keeps a small status object (update-status.json, see STATUS_NAME): when the last update
started and finished and whether it brought new data. The page reads it after "Update data" to say how far the update is.

The version of an object is its GCS "generation". An upload carries "only if the generation is still N", so two jobs running
at the same time cannot overwrite each other: the second one fails and nothing is lost.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


class ConcurrentUpdate(RuntimeError):
    """The object was changed by someone else since it was pulled; nothing was uploaded."""


class ObjectStore(Protocol):
    def generation(self, uri: str) -> int:
        """The current generation of the object, 0 if it does not exist."""

    def download(self, uri: str, path: Path) -> int:
        """Download the object to `path` (replaced atomically) and return the generation that was downloaded."""

    def upload(self, uri: str, path: Path, if_generation_match: int) -> int:
        """Upload `path` only if the object's generation is still `if_generation_match` (0: it must not exist). Returns the new generation."""

    def read_text(self, uri: str) -> str | None:
        """The text of a small object, None if it does not exist."""

    def write_text(self, uri: str, text: str) -> None:
        """Replace a small text object."""


def split_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://") or "/" not in uri[5:]:
        raise ValueError(f"Expected gs://bucket/object, got {uri!r}")
    bucket, name = uri[5:].split("/", 1)
    return bucket, name


class GcsObjectStore:
    """The real thing, using the google-cloud-storage library (installed with the `cloud` extra)."""

    def __init__(self):
        from google.cloud import storage

        self._client = storage.Client()

    def _blob(self, uri: str):
        bucket, name = split_uri(uri)
        return self._client.bucket(bucket).get_blob(name)

    def generation(self, uri: str) -> int:
        blob = self._blob(uri)
        return blob.generation if blob else 0

    def download(self, uri: str, path: Path) -> int:
        blob = self._blob(uri)
        if blob is None:
            raise FileNotFoundError(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        blob.download_to_filename(str(tmp), if_generation_match=blob.generation)
        os.replace(tmp, path)
        return blob.generation

    def upload(self, uri: str, path: Path, if_generation_match: int) -> int:
        from google.api_core.exceptions import PreconditionFailed

        bucket, name = split_uri(uri)
        blob = self._client.bucket(bucket).blob(name)
        try:
            blob.upload_from_filename(str(path), content_type="application/vnd.sqlite3", if_generation_match=if_generation_match)
        except PreconditionFailed as e:
            raise ConcurrentUpdate(f"{uri} was changed by someone else since it was downloaded; nothing was uploaded") from e
        return blob.generation

    def read_text(self, uri: str) -> str | None:
        blob = self._blob(uri)
        return blob.download_as_text() if blob else None

    def write_text(self, uri: str, text: str) -> None:
        bucket, name = split_uri(uri)
        self._client.bucket(bucket).blob(name).upload_from_string(text, content_type="application/json")


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


class RemoteDatabase:
    """A local SQLite file and the GCS object it is a copy of."""

    def __init__(self, uri: str, local: str | Path, store: ObjectStore | None = None):
        split_uri(uri)
        self.uri = uri
        self.local = Path(local)
        self._store = store
        self._marker = self.local.with_name(self.local.name + ".generation")  # which generation the local copy is
        self._pulled_sha = ""

    @property
    def store(self) -> ObjectStore:
        if self._store is None:
            self._store = GcsObjectStore()
        return self._store

    @classmethod
    def from_env(cls, default_local: str | Path, store: ObjectStore | None = None) -> "RemoteDatabase | None":
        uri = os.environ.get("VALUATION_DB_URI", "").strip()
        return cls(uri, default_local, store) if uri else None

    @property
    def local_generation(self) -> int:
        try:
            return int(self._marker.read_text().strip())
        except (OSError, ValueError):
            return 0

    def pull(self) -> bool:
        """Download the object if it is not the version the local copy is. Returns whether the local file was replaced.

        If the object does not exist yet there is nothing to download: the local file (if any) is kept, and the first push creates the object.
        """
        remote = self.store.generation(self.uri)
        if remote == 0:
            self._marker.unlink(missing_ok=True)
            self._pulled_sha = file_sha(self.local)
            return False
        if remote == self.local_generation and self.local.exists():
            self._pulled_sha = file_sha(self.local)
            return False
        generation = self.store.download(self.uri, self.local)
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self._marker.write_text(str(generation))
        self._pulled_sha = file_sha(self.local)
        return True

    def push(self) -> bool:
        """Upload the local file if it changed since the last pull. Returns whether anything was uploaded.

        Raises ConcurrentUpdate if the object was replaced by someone else in the meantime.
        """
        if not self.local.exists() or file_sha(self.local) == self._pulled_sha:
            return False
        generation = self.store.upload(self.uri, self.local, if_generation_match=self.local_generation)
        self._marker.write_text(str(generation))
        self._pulled_sha = file_sha(self.local)
        return True


STATUS_NAME = "update-status.json"

# results an update can end with (the "result" field of the status object; "running" while it runs)
RUNNING, NEW_DATA, NO_NEW_DATA, FAILED = "running", "new_data", "no_new_data", "failed"


def status_uri(db_uri: str) -> str:
    """The status object that sits next to the database object (same bucket and folder)."""
    bucket, name = split_uri(db_uri)
    folder = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
    return f"gs://{bucket}/{folder}{STATUS_NAME}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str | None) -> datetime | None:
    try:
        return datetime.strptime(text or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def write_status(store: ObjectStore, uri: str, status: dict) -> None:
    store.write_text(uri, json.dumps(status, sort_keys=True))


def read_status(store: ObjectStore, uri: str) -> dict | None:
    """The last status the update job wrote, None if there is none or it cannot be read (the page then just says it is updating)."""
    try:
        text = store.read_text(uri)
        status = json.loads(text) if text else None
    except Exception as e:  # a missing permission or a broken object must not break the page
        print(f"Could not read the update status {uri}: {e}", file=sys.stderr)
        return None
    return status if isinstance(status, dict) else None


def start_job(job: str, session=None) -> str:
    """Start an execution of a Cloud Run job (job = projects/PROJECT/locations/REGION/jobs/NAME) and return the execution's operation name.

    Uses the credentials of the service the page runs as, which needs the Cloud Run Invoker role on the job.
    """
    if session is None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        session = AuthorizedSession(credentials)
    response = session.post(f"https://run.googleapis.com/v2/{job}:run", json={}, timeout=30)
    response.raise_for_status()
    return response.json().get("name", "")
