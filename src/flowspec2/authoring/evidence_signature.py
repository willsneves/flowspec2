"""Detached Ed25519 authentication for verified authoring evidence."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import jsonschema
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from flowspec2.json_codec import strict_json_loads

from .evidence_verification import (
    AuthoringEvidenceVerification,
    verify_authoring_evidence,
)

AUTHORING_EVIDENCE_SIGNATURE_FORMAT: Final[str] = "flowspec2/authoring-evidence-signature@1"
AUTHORING_EVIDENCE_SIGNATURE_ALGORITHM: Final[str] = "ed25519"
_SIGNATURE_SCHEMA_PATH: Final[Path] = Path(__file__).with_name(
    "authoring-evidence-signature.schema.json"
)
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_ED25519_SIGNATURE_BYTES: Final[int] = 64
_SIGNATURE_MESSAGE_PREFIX: Final[bytes] = b"flowspec2/authoring-evidence-signature@1\x00ed25519\x00"
_signature_schema_cache: dict[str, Any] | None = None


def _canonical_json(json_document: object) -> str:
    return json.dumps(
        json_document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _signature_schema() -> dict[str, Any]:
    global _signature_schema_cache
    cached_schema = _signature_schema_cache
    if cached_schema is None:
        decoded_schema = strict_json_loads(_SIGNATURE_SCHEMA_PATH.read_text(encoding="utf-8"))
        if not isinstance(decoded_schema, dict):
            raise ValueError("authoring evidence signature schema must be a JSON object")
        jsonschema.Draft202012Validator.check_schema(decoded_schema)
        cached_schema = cast(dict[str, Any], decoded_schema)
        _signature_schema_cache = cached_schema
    return cached_schema


def authoring_evidence_signature_schema() -> dict[str, Any]:
    """Return an owned copy of the detached-signature schema."""

    return copy.deepcopy(_signature_schema())


def _public_key_id(public_key: Ed25519PublicKey) -> str:
    public_key_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(public_key_bytes).hexdigest()


def _signature_message(evidence_digest: str) -> bytes:
    if not _DIGEST_PATTERN.fullmatch(evidence_digest):
        raise ValueError("signed evidence digest must be lowercase SHA-256")
    return _SIGNATURE_MESSAGE_PREFIX + evidence_digest.encode("ascii")


def _load_private_key(private_key_pem: bytes) -> Ed25519PrivateKey:
    try:
        private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    except (TypeError, UnsupportedAlgorithm, ValueError) as key_error:
        raise ValueError("private key must be unencrypted PKCS8 PEM") from key_error
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("private key must use Ed25519")
    return private_key


def _load_public_key(public_key_pem: bytes) -> Ed25519PublicKey:
    try:
        public_key = serialization.load_pem_public_key(public_key_pem)
    except (TypeError, UnsupportedAlgorithm, ValueError) as key_error:
        raise ValueError("public key must be SubjectPublicKeyInfo PEM") from key_error
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("public key must use Ed25519")
    return public_key


@dataclass(frozen=True)
class AuthoringEvidenceSignature:
    """Canonical detached signature for one verified evidence digest."""

    evidence_digest: str
    key_id: str
    signature_base64: str

    def __post_init__(self) -> None:
        if not _DIGEST_PATTERN.fullmatch(self.evidence_digest):
            raise ValueError("signature evidence digest must be lowercase SHA-256")
        if not _DIGEST_PATTERN.fullmatch(self.key_id):
            raise ValueError("signature key identifier must be lowercase SHA-256")
        try:
            signature_bytes = base64.b64decode(self.signature_base64, validate=True)
        except (binascii.Error, ValueError) as decoding_error:
            raise ValueError("signature must be canonical base64") from decoding_error
        if (
            len(signature_bytes) != _ED25519_SIGNATURE_BYTES
            or base64.b64encode(signature_bytes).decode("ascii") != self.signature_base64
        ):
            raise ValueError("signature must be a canonical Ed25519 signature")

    @property
    def signature_bytes(self) -> bytes:
        return base64.b64decode(self.signature_base64, validate=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "format": AUTHORING_EVIDENCE_SIGNATURE_FORMAT,
            "algorithm": AUTHORING_EVIDENCE_SIGNATURE_ALGORITHM,
            "evidence_digest": self.evidence_digest,
            "key_id": self.key_id,
            "signature": self.signature_base64,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, serialized_signature: str) -> AuthoringEvidenceSignature:
        """Parse one strict canonical detached signature."""

        decoded_signature = strict_json_loads(serialized_signature)
        jsonschema.Draft202012Validator(_signature_schema()).validate(decoded_signature)
        if not isinstance(decoded_signature, dict):
            raise AssertionError("the signature schema accepted a non-object document")
        if serialized_signature.strip() != _canonical_json(decoded_signature):
            raise ValueError("authoring evidence signature must use canonical compact JSON")
        return cls(
            evidence_digest=cast(str, decoded_signature["evidence_digest"]),
            key_id=cast(str, decoded_signature["key_id"]),
            signature_base64=cast(str, decoded_signature["signature"]),
        )


@dataclass(frozen=True)
class AuthoringEvidenceAuthentication:
    """A verified evidence report authenticated by one trusted public key."""

    evidence: AuthoringEvidenceVerification
    key_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "authenticated": True,
            "algorithm": AUTHORING_EVIDENCE_SIGNATURE_ALGORITHM,
            "key_id": self.key_id,
            "evidence": self.evidence.to_dict(),
        }


def sign_authoring_evidence(
    serialized_evidence: str,
    private_key_pem: bytes,
    *,
    expected_repository_revision: str | None = None,
) -> AuthoringEvidenceSignature:
    """Verify evidence and create its deterministic detached signature."""

    evidence_verification = verify_authoring_evidence(
        serialized_evidence,
        expected_repository_revision=expected_repository_revision,
    )
    private_key = _load_private_key(private_key_pem)
    signature_bytes = private_key.sign(_signature_message(evidence_verification.digest))
    return AuthoringEvidenceSignature(
        evidence_digest=evidence_verification.digest,
        key_id=_public_key_id(private_key.public_key()),
        signature_base64=base64.b64encode(signature_bytes).decode("ascii"),
    )


def verify_authoring_evidence_signature(
    serialized_evidence: str,
    serialized_signature: str,
    public_key_pem: bytes,
    *,
    expected_repository_revision: str | None = None,
) -> AuthoringEvidenceAuthentication:
    """Verify evidence and authenticate its digest with a trusted public key."""

    evidence_verification = verify_authoring_evidence(
        serialized_evidence,
        expected_repository_revision=expected_repository_revision,
    )
    evidence_signature = AuthoringEvidenceSignature.from_json(serialized_signature)
    if evidence_signature.evidence_digest != evidence_verification.digest:
        raise ValueError("authoring evidence signature names a different evidence digest")
    public_key = _load_public_key(public_key_pem)
    public_key_id = _public_key_id(public_key)
    if evidence_signature.key_id != public_key_id:
        raise ValueError("authoring evidence signature names a different public key")
    try:
        public_key.verify(
            evidence_signature.signature_bytes,
            _signature_message(evidence_verification.digest),
        )
    except InvalidSignature as signature_error:
        raise ValueError("authoring evidence signature is invalid") from signature_error
    return AuthoringEvidenceAuthentication(evidence=evidence_verification, key_id=public_key_id)
