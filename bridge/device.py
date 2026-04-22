"""Ed25519 device identity management — matches OpenClaw's device-identity.ts exactly."""

import json
import os
import hashlib
import base64
import time
import struct

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def _base64url_encode(data: bytes) -> str:
    """Base64url encode without padding."""
    return base64.b64encode(data).replace(b"+", b"-").replace(b"/", b"_").rstrip(b"=").decode()


def _base64url_decode(s: str) -> bytes:
    """Base64url decode with padding restoration."""
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.b64decode(s)


# Ed25519 SPKI DER prefix (30 2a 30 05 06 03 2b 65 70 03 21 00)
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def _raw_public_key(private_key: Ed25519PrivateKey) -> bytes:
    """Extract raw 32-byte Ed25519 public key."""
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _fingerprint(raw_pub: bytes) -> str:
    """SHA-256 hex digest of raw public key bytes → deviceId."""
    return hashlib.sha256(raw_pub).hexdigest()


def load_or_create_keys(path: str):
    """Load or generate Ed25519 keypair, persist to JSON file.

    Returns (private_key, device_id) where device_id = sha256(raw_pubkey).hex()
    """
    raw_pub = None
    private_key = None

    if os.path.exists(path):
        try:
            data = json.load(open(path))
            if data.get("version") == 1 and "privateKeyPem" in data:
                private_key = serialization.load_pem_private_key(
                    data["privateKeyPem"].encode(), password=None
                )
                raw_pub = _raw_public_key(private_key)
                return private_key, _fingerprint(raw_pub)
        except Exception:
            pass

    # Generate new
    private_key = Ed25519PrivateKey.generate()
    raw_pub = _raw_public_key(private_key)

    # Export PEM (matches OpenClaw format)
    pub_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    priv_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()

    stored = {
        "version": 1,
        "deviceId": _fingerprint(raw_pub),
        "publicKeyPem": pub_pem,
        "privateKeyPem": priv_pem,
        "createdAtMs": int(time.time() * 1000),
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(stored, f, indent=2)
        f.write("\n")
    os.chmod(path, 0o600)

    return private_key, _fingerprint(raw_pub)


def build_device_auth(private_key: Ed25519PrivateKey, nonce: str, token: str,
                          client_id: str = "cli", client_mode: str = "cli"):
    """
    Build the device auth object for the connect handshake.

    Payload format (v3, pipe-separated):
      v3|deviceId|clientId|clientMode|role|scopes|signedAtMs|token|nonce|platform|deviceFamily

    All fields must match what OpenClaw expects exactly.
    """
    raw_pub = _raw_public_key(private_key)
    device_id = _fingerprint(raw_pub)

    signed_at_ms = int(time.time() * 1000)

    role = "operator"
    scopes = "operator.read,operator.write"
    platform = "linux"
    device_family = ""  # normalized to empty string

    payload = "|".join([
        "v3",
        device_id,
        client_id,
        client_mode,
        role,
        scopes,
        str(signed_at_ms),
        token,
        nonce,
        platform,
        device_family,
    ])

    # Sign with Ed25519 → base64url
    sig_bytes = private_key.sign(payload.encode("utf-8"))
    signature = _base64url_encode(sig_bytes)

    # Public key → base64url of raw 32 bytes
    public_key_b64url = _base64url_encode(raw_pub)

    return {
        "id": device_id,
        "publicKey": public_key_b64url,
        "signature": signature,
        "signedAt": signed_at_ms,
        "nonce": nonce,
    }
