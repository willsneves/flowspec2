"""Internal detached Ed25519 signing and verification primitives."""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ED25519_SIGNATURE_BYTES: Final[int] = 64


class DetachedEd25519KeyMismatchError(ValueError):
    """A detached signature names a different trusted public key."""


class DetachedEd25519VerificationError(ValueError):
    """A detached signature does not authenticate its message."""


@dataclass(frozen=True)
class DetachedEd25519Signature:
    """Canonical signature material produced by one Ed25519 private key."""

    key_id: str
    signature_base64: str


def load_ed25519_private_key(private_key_pem: bytes) -> Ed25519PrivateKey:
    """Load one unencrypted PKCS8 Ed25519 private key."""

    try:
        private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    except (TypeError, UnsupportedAlgorithm, ValueError) as key_error:
        raise ValueError("private key must be unencrypted PKCS8 PEM") from key_error
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("private key must use Ed25519")
    return private_key


def load_ed25519_public_key(public_key_pem: bytes) -> Ed25519PublicKey:
    """Load one SubjectPublicKeyInfo Ed25519 public key."""

    try:
        public_key = serialization.load_pem_public_key(public_key_pem)
    except (TypeError, UnsupportedAlgorithm, ValueError) as key_error:
        raise ValueError("public key must be SubjectPublicKeyInfo PEM") from key_error
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("public key must use Ed25519")
    return public_key


def ed25519_public_key_id(public_key: Ed25519PublicKey) -> str:
    """Return the lowercase SHA-256 identifier of a raw Ed25519 public key."""

    public_key_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(public_key_bytes).hexdigest()


def canonical_ed25519_signature_bytes(signature_base64: str) -> bytes:
    """Decode and validate one canonical base64 Ed25519 signature."""

    try:
        signature_bytes = base64.b64decode(signature_base64, validate=True)
    except (binascii.Error, ValueError) as decoding_error:
        raise ValueError("signature must be canonical base64") from decoding_error
    if (
        len(signature_bytes) != ED25519_SIGNATURE_BYTES
        or base64.b64encode(signature_bytes).decode("ascii") != signature_base64
    ):
        raise ValueError("signature must be a canonical Ed25519 signature")
    return signature_bytes


def sign_detached_ed25519(
    message: bytes,
    private_key_pem: bytes,
) -> DetachedEd25519Signature:
    """Sign bytes and return canonical detached Ed25519 signature material."""

    private_key = load_ed25519_private_key(private_key_pem)
    signature_bytes = private_key.sign(message)
    return DetachedEd25519Signature(
        key_id=ed25519_public_key_id(private_key.public_key()),
        signature_base64=base64.b64encode(signature_bytes).decode("ascii"),
    )


def verify_detached_ed25519(
    message: bytes,
    signature_base64: str,
    public_key_pem: bytes,
    *,
    expected_key_id: str,
) -> str:
    """Authenticate bytes with the named trusted Ed25519 public key."""

    signature_bytes = canonical_ed25519_signature_bytes(signature_base64)
    public_key = load_ed25519_public_key(public_key_pem)
    public_key_id = ed25519_public_key_id(public_key)
    if public_key_id != expected_key_id:
        raise DetachedEd25519KeyMismatchError("detached signature names a different public key")
    try:
        public_key.verify(signature_bytes, message)
    except InvalidSignature as signature_error:
        raise DetachedEd25519VerificationError(
            "detached Ed25519 signature is invalid"
        ) from signature_error
    return public_key_id
