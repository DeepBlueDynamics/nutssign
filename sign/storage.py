"""Envelope storage: local disk, optionally mirrored to a GCS bucket.

Layout (local and in the bucket under GCS_PREFIX/):
    <envelope_id>/envelope.json
    <envelope_id>/source.pdf
    <envelope_id>/signed.pdf
    <envelope_id>/pages/page-<n>.png
    <envelope_id>/signatures/<signer_id>.png
    index.json                      token -> envelope id
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import Envelope, Signer

log = logging.getLogger("nuts-sign.storage")


class Storage:
    def __init__(self, root: Path, bucket: str = "", prefix: str = "envelopes"):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.prefix = prefix
        self.bucket = None
        if bucket:
            from google.cloud import storage as gcs  # imported lazily so local runs need no credentials

            self.bucket = gcs.Client().bucket(bucket)
            log.info("GCS mirror enabled: gs://%s/%s", bucket, prefix)
            self._pull_index()

    # ----- low level
    def _blob(self, rel: str):
        return self.bucket.blob(f"{self.prefix}/{rel}")

    def local(self, rel: str) -> Path:
        return self.root / rel

    def write_bytes(self, rel: str, data: bytes) -> None:
        p = self.local(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
        if self.bucket is not None:
            self._blob(rel).upload_from_string(data)

    def read_bytes(self, rel: str) -> Optional[bytes]:
        p = self.local(rel)
        if p.exists():
            return p.read_bytes()
        if self.bucket is not None:
            blob = self._blob(rel)
            if blob.exists():
                data = blob.download_as_bytes()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                return data
        return None

    def exists(self, rel: str) -> bool:
        if self.local(rel).exists():
            return True
        return self.bucket is not None and self._blob(rel).exists()

    # ----- envelopes
    def save(self, env: Envelope) -> None:
        with self.lock:
            self.write_bytes(f"{env.id}/envelope.json", env.model_dump_json(indent=2).encode())
            index = self._load_index()
            changed = False
            for tok in [s.token for s in env.signers] + [env.admin_token]:
                if index.get(tok) != env.id:
                    index[tok] = env.id
                    changed = True
            if changed:
                self.write_bytes("index.json", json.dumps(index, indent=1).encode())

    def load(self, env_id: str) -> Optional[Envelope]:
        if not env_id or "/" in env_id or ".." in env_id:
            return None
        data = self.read_bytes(f"{env_id}/envelope.json")
        if not data:
            return None
        env = Envelope.model_validate_json(data)
        if b'"admin_token"' not in data:  # envelope from before management links existed: persist the new token
            self.save(env)
        return env

    def find_by_admin_token(self, token: str) -> Optional[Envelope]:
        if not token or len(token) < 20:
            return None
        env_id = self._load_index().get(token)
        env = self.load(env_id) if env_id else None
        if env is None:
            env = next((e for e in self.list_envelopes() if secrets.compare_digest(e.admin_token, token)), None)
        if env is None or not secrets.compare_digest(env.admin_token, token):
            return None
        return env

    def list_ids(self) -> List[str]:
        ids = {p.name for p in self.root.iterdir() if p.is_dir() and (p / "envelope.json").exists()}
        if self.bucket is not None:
            it = self.bucket.client.list_blobs(self.bucket, prefix=f"{self.prefix}/", delimiter="/")
            list(it)  # consume to populate prefixes
            for pre in it.prefixes:
                ids.add(pre[len(self.prefix) + 1:].strip("/"))
        return sorted(ids)

    def list_envelopes(self) -> List[Envelope]:
        envs = [self.load(i) for i in self.list_ids()]
        envs = [e for e in envs if e]
        envs.sort(key=lambda e: e.created_at, reverse=True)
        return envs

    def find_by_token(self, token: str) -> Optional[Tuple[Envelope, Signer]]:
        if not token or len(token) < 20:
            return None
        env_id = self._load_index().get(token)
        env = self.load(env_id) if env_id else None
        if env is None:  # index miss: fall back to a scan (cheap at this scale)
            for e in self.list_envelopes():
                if e.signer_by_token(token):
                    env = e
                    break
        if env is None:
            return None
        signer = env.signer_by_token(token)
        return (env, signer) if signer else None

    # ----- index
    def _load_index(self) -> Dict[str, str]:
        data = self.read_bytes("index.json")
        if not data:
            return {}
        try:
            return json.loads(data)
        except ValueError:
            return {}

    def _pull_index(self) -> None:
        try:
            self.read_bytes("index.json")
        except Exception as exc:  # bucket reachable but empty is fine; anything else is loud
            log.warning("could not read index from bucket: %s", exc)
