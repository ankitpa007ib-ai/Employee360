import base64
import hashlib
import hmac
import json
import re
import secrets
import time


PBKDF2_ITERATIONS = 310_000
MOBILE_PATTERN = re.compile(r"^\d{10}$")


def hash_mobile_number(mobile_number: str) -> str:
    if not MOBILE_PATTERN.fullmatch(mobile_number):
        raise ValueError("Mobile number must contain exactly 10 digits.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", mobile_number.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_mobile_number(mobile_number: str, stored_hash: str | None) -> bool:
    if not stored_hash or not MOBILE_PATTERN.fullmatch(mobile_number):
        return False
    try:
        algorithm, iterations_text, salt_hex, digest_hex = stored_hash.split("$", 3)
        iterations = int(iterations_text)
        if algorithm != "pbkdf2_sha256" or iterations != PBKDF2_ITERATIONS:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", mobile_number.encode(), salt, iterations)
    return hmac.compare_digest(actual, expected)


def create_session_token(employee_id: str, secret: str, ttl_seconds: int = 28_800) -> str:
    payload = json.dumps(
        {"sub": employee_id, "exp": int(time.time()) + ttl_seconds},
        separators=(",", ":"),
    ).encode()
    encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    signature = hmac.new(secret.encode(), encoded_payload.encode(), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{encoded_payload}.{encoded_signature}"


def read_session_token(token: str | None, secret: str) -> str | None:
    if not token:
        return None
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(secret.encode(), encoded_payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None

        payload_bytes = base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
        payload = json.loads(payload_bytes)
        if not isinstance(payload.get("sub"), str) or payload.get("exp", 0) < int(time.time()):
            return None
        return payload["sub"]
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
