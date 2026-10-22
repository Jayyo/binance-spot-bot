"""API credentials: where they come from and how they are kept out of output.

- Paper and backtest modes never call into this module.
- Testnet and live use different variable names, so a testnet run cannot pick
  up live keys by accident:

    live:    BINANCE_API_KEY + (BINANCE_PRIVATE_KEY_PATH | BINANCE_API_SECRET)
    testnet: BINANCE_TESTNET_API_KEY + (BINANCE_TESTNET_PRIVATE_KEY_PATH | BINANCE_TESTNET_API_SECRET)

- Values come from the process environment, then from ./.env for anything not
  already set. The environment always wins.
- The secret or private key never leaves this process: requests carry the API
  key (an identifier) and a signature, nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dotenv import dotenv_values

log = logging.getLogger(__name__)

MODES = ("testnet", "live")
TOKEN = re.compile(r"[A-Za-z0-9]{16,128}")


class CredentialError(Exception):
    pass


class Secret:
    """A string that does not show up in repr(), str() or tracebacks of containers."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __reduce__(self):  # refuse pickling, which would write the value somewhere
        raise TypeError("Secret cannot be pickled")


class Signer(Protocol):
    kind: str

    def sign(self, payload: bytes) -> str: ...


class HmacSigner:
    kind = "hmac"

    def __init__(self, secret: Secret) -> None:
        self._secret = secret

    def sign(self, payload: bytes) -> str:
        return hmac.new(self._secret.reveal().encode(), payload, hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return "HmacSigner(***)"


class KeyPairSigner:
    """Ed25519 (recommended by Binance) or RSA private key loaded from a PEM file."""

    def __init__(self, private_key: object) -> None:
        from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

        if isinstance(private_key, ed25519.Ed25519PrivateKey):
            self.kind = "ed25519"
        elif isinstance(private_key, rsa.RSAPrivateKey):
            self.kind = "rsa"
        else:
            raise CredentialError("private key must be Ed25519 or RSA")
        self._key = private_key

    def sign(self, payload: bytes) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        if self.kind == "ed25519":
            raw = self._key.sign(payload)  # type: ignore[attr-defined]
        else:
            raw = self._key.sign(payload, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[attr-defined]
        return base64.b64encode(raw).decode()

    def __repr__(self) -> str:
        return f"KeyPairSigner({self.kind}, ***)"


@dataclass(frozen=True)
class Credentials:
    mode: str
    api_key: Secret
    signer: Signer
    source: str

    @property
    def fingerprint(self) -> str:
        """Short hash of the API key, safe to print and store."""
        return hashlib.sha256(self.api_key.reveal().encode()).hexdigest()[:12]

    def secrets(self) -> list[str]:
        values = [self.api_key.reveal()]
        if isinstance(self.signer, HmacSigner):
            values.append(self.signer._secret.reveal())
        return values


def env_names(mode: str) -> tuple[str, str, str, str]:
    if mode not in MODES:
        raise ValueError(f"credentials are only used in {MODES}, not {mode!r}")
    p = "BINANCE_TESTNET_" if mode == "testnet" else "BINANCE_"
    return p + "API_KEY", p + "API_SECRET", p + "PRIVATE_KEY_PATH", p + "PRIVATE_KEY_PASSWORD"


def _loose_permissions(path: Path) -> bool:
    if sys.platform == "win32":
        return False
    return bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def load_credentials(
    mode: str,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> Credentials:
    key_var, secret_var, path_var, pass_var = env_names(mode)
    env = dict(os.environ if environ is None else environ)
    dotenv_path = Path(".env") if dotenv_path is None else dotenv_path
    from_file: dict[str, str] = {}
    if dotenv_path.is_file():
        if _loose_permissions(dotenv_path):
            log.warning(
                "%s is readable by other users; run: chmod 600 %s", dotenv_path, dotenv_path
            )
        from_file = {k: v for k, v in dotenv_values(dotenv_path).items() if v}

    def get(name: str) -> tuple[str | None, str]:
        if env.get(name):
            return env[name], "environment"
        if from_file.get(name):
            return from_file[name], str(dotenv_path)
        return None, ""

    api_key, key_source = get(key_var)
    secret, _ = get(secret_var)
    key_path, _ = get(path_var)
    password, _ = get(pass_var)

    hint = (
        f"set {key_var} and either {path_var} (Ed25519 PEM file, recommended) or {secret_var} "
        f"(HMAC) in the environment or in .env; see docs/api-keys.md"
    )
    if not api_key:
        raise CredentialError(f"no {mode} API key found: {hint}")
    for name, value in ((key_var, api_key), (secret_var, secret)):
        # Binance keys are plain alphanumerics. Anything else is usually a stray quote,
        # space or newline from copy-paste; the value itself is never printed.
        if value is not None and not TOKEN.fullmatch(value):
            raise CredentialError(
                f"{name} contains characters a Binance key never has (quote, space?)"
            )
    if secret and key_path:
        raise CredentialError(f"both {secret_var} and {path_var} are set; keep exactly one")
    if not secret and not key_path:
        raise CredentialError(f"{key_var} is set but no signing secret was found: {hint}")

    signer: Signer
    if secret:
        signer = HmacSigner(Secret(secret))
    else:
        assert key_path is not None
        signer = KeyPairSigner(_read_private_key(Path(key_path).expanduser(), password))
    return Credentials(mode=mode, api_key=Secret(api_key), signer=signer, source=key_source)


def _read_private_key(path: Path, password: str | None) -> object:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    if not path.is_file():
        raise CredentialError(f"private key file not found: {path}")
    if _loose_permissions(path):
        log.warning("%s is readable by other users; run: chmod 600 %s", path, path)
    try:
        return load_pem_private_key(path.read_bytes(), password.encode() if password else None)
    except (ValueError, TypeError) as exc:
        # The message never includes key material, only why loading failed.
        raise CredentialError(f"could not load private key {path}: {exc}") from None


class RedactingFilter(logging.Filter):
    """Last line of defence: blank out secret values if they ever reach a log record."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for s in self._secrets:
            redacted = redacted.replace(s, "***")
        if redacted != message:
            record.msg, record.args = redacted, None
        return True
