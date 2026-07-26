import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

from .errors import SurfaceError


_TOKEN_VERSION = "dn1"
_DEFAULT_TTL_SECONDS = 3600


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class ProvenanceStore:
    def __init__(self, cache_dir: Path, ttl_seconds: int = _DEFAULT_TTL_SECONDS):
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_seconds
        self._secret: bytes | None = None

    def _key(self) -> bytes:
        if self._secret is not None:
            return self._secret
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / ".provenance-key"
        if not path.exists():
            # Creating the file and filling it are two steps, so a second
            # process could read the empty file in between and reject a
            # perfectly good key. Write it elsewhere and move it into place,
            # which other processes only ever observe as complete.
            temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(os.urandom(32))
                os.replace(temporary, path)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        secret = path.read_bytes()
        if len(secret) != 32:
            raise SurfaceError("provenance signing key is invalid", retryable=False)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self._secret = secret
        return secret

    def issue(self, kind: str, payload: dict[str, Any]) -> str:
        now = int(time.time())
        body = {
            "kind": kind,
            "issued_at": now,
            "expires_at": now + self.ttl_seconds,
            **payload,
        }
        encoded = _b64encode(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        )
        signature = _b64encode(
            hmac.new(self._key(), encoded.encode(), hashlib.sha256).digest()
        )
        return f"{_TOKEN_VERSION}.{encoded}.{signature}"

    def verify(self, token: str, kind: str) -> dict[str, Any]:
        try:
            version, encoded, signature = token.split(".", 2)
            if version != _TOKEN_VERSION:
                raise ValueError("unsupported token version")
            expected = hmac.new(self._key(), encoded.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _b64decode(signature)):
                raise ValueError("signature mismatch")
            payload = json.loads(_b64decode(encoded))
            if payload.get("kind") != kind:
                raise ValueError("token kind mismatch")
            if int(payload["expires_at"]) < int(time.time()):
                raise ValueError("token expired")
            return payload
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise SurfaceError(
                f"invalid or expired {kind} provenance token",
                details={"kind": kind},
            ) from error

    def _state_path(self, target: str) -> Path:
        digest = hashlib.sha256(target.encode()).hexdigest()
        return self.cache_dir / "observations" / f"{digest}.json"

    def remember(self, target: str, observation_id: str, surface_id: str) -> None:
        path = self._state_path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}")
        temporary.write_text(
            json.dumps(
                {
                    "target": target,
                    "observation_id": observation_id,
                    "surface_id": surface_id,
                },
                separators=(",", ":"),
            )
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)

    def latest(self, target: str) -> dict[str, str] | None:
        try:
            payload = json.loads(self._state_path(target).read_text())
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if payload.get("target") != target:
            return None
        observation_id = payload.get("observation_id")
        surface_id = payload.get("surface_id")
        if not isinstance(observation_id, str) or not isinstance(surface_id, str):
            return None
        return {"observation_id": observation_id, "surface_id": surface_id}
