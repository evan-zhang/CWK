"""Stdlib-only ECDSA P-256 / SHA-256 signing for PR-001 release authorization (G7).

Why this module exists
----------------------
The G7 authorization receipt carries a DETACHED external signature. The
independent review rejected the previous fixture design, which "verified"
signatures by looking the digest up in a whitelist of blessed digests. That is
not verification: it accepts any body whose digest happens to be listed, and it
cannot distinguish a real signer from a test harness that edited the list.

So the tests need to actually sign and actually verify. The reference host is
LibreSSL 3.3.6 with Python 3.11 and neither ``cryptography`` nor ``PyNaCl``
installed, and the standing constraint is that no new Python package may be
installed. Ed25519 is therefore unavailable at both layers, which is exactly
why the schema froze ``ecdsa-p256-sha256`` as the only permitted algorithm.
P-256 needs nothing but integer arithmetic and ``hashlib``/``hmac``, so it is
implemented here directly.

Three properties this module is responsible for
-----------------------------------------------
1. **Low-S normalisation.** ECDSA is malleable: for a valid ``(r, s)`` the pair
   ``(r, n - s)`` verifies over the same message under the same key. Since
   ``authorization_sha256`` covers ``signature_b64``, an un-normalised signature
   lets an attacker mint a second, differently-hashed authorization for one
   unchanged decision. Signing always emits ``s <= n/2``; verification rejects
   high-S unless a test explicitly asks for the malleability negative.

2. **Canonical DER.** Strict encode and strict decode. Leading zero padding,
   non-minimal length octets, long-form lengths that should be short-form,
   indefinite length, and trailing bytes are all rejected rather than tolerated,
   because each of them is a second encoding of the same signature and so a
   second ``authorization_sha256``.

3. **Fail closed, never skip.** ``require_openssl()`` raises
   :class:`SigningDependencyError` when the interop binary is missing. Nothing
   in here degrades to "verification unavailable, assume valid", and no caller
   is offered a "skip" return value for a verification it could not perform.

Everything is deterministic: keys come from a caller-supplied seed and ``k``
comes from RFC 6979, so fixtures are reproducible without touching entropy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import shutil
import subprocess
import tempfile

# --------------------------------------------------------------------------
# NIST P-256 (secp256r1 / prime256v1) domain parameters
# --------------------------------------------------------------------------

P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFC
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5

HALF_N = N // 2
CURVE_BYTES = 32

ALGORITHM = "ecdsa-p256-sha256"

# ASN.1 OIDs, DER-encoded value bytes (tag/length added by the writer).
_OID_EC_PUBLIC_KEY = bytes([0x2A, 0x86, 0x48, 0xCE, 0x3D, 0x02, 0x01])  # 1.2.840.10045.2.1
_OID_PRIME256V1 = bytes([0x2A, 0x86, 0x48, 0xCE, 0x3D, 0x03, 0x01, 0x07])  # 1.2.840.10045.3.1.7


class SigningError(Exception):
    """A signature, key or encoding was structurally or semantically invalid."""


class SigningDependencyError(SigningError):
    """A required external verification dependency is unavailable.

    Raised instead of skipping. A test suite that silently stops verifying
    signatures when a binary is missing reports the same green result whether
    the crypto works or not, which is the failure mode this whole layer exists
    to prevent.
    """


# --------------------------------------------------------------------------
# Curve arithmetic (Jacobian projective coordinates)
# --------------------------------------------------------------------------


def _inv_mod(value: int, modulus: int) -> int:
    if value % modulus == 0:
        raise SigningError("inverse of zero")
    return pow(value % modulus, modulus - 2, modulus)


def _jacobian_double(point):
    x1, y1, z1 = point
    if y1 == 0 or z1 == 0:
        return (0, 0, 0)
    ysq = (y1 * y1) % P
    zsq = (z1 * z1) % P
    s = (4 * x1 * ysq) % P
    m = (3 * x1 * x1 + A * zsq * zsq) % P
    x3 = (m * m - 2 * s) % P
    y3 = (m * (s - x3) - 8 * ysq * ysq) % P
    z3 = (2 * y1 * z1) % P
    return (x3, y3, z3)


def _jacobian_add(p1, p2):
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    if z1 == 0:
        return p2
    if z2 == 0:
        return p1
    z1sq = (z1 * z1) % P
    z2sq = (z2 * z2) % P
    u1 = (x1 * z2sq) % P
    u2 = (x2 * z1sq) % P
    s1 = (y1 * z2sq * z2) % P
    s2 = (y2 * z1sq * z1) % P
    if u1 == u2:
        if s1 != s2:
            return (0, 0, 0)
        return _jacobian_double(p1)
    h = (u2 - u1) % P
    r = (s2 - s1) % P
    hsq = (h * h) % P
    hcu = (h * hsq) % P
    u1hsq = (u1 * hsq) % P
    x3 = (r * r - hcu - 2 * u1hsq) % P
    y3 = (r * (u1hsq - x3) - s1 * hcu) % P
    z3 = (h * z1 * z2) % P
    return (x3, y3, z3)


def _jacobian_multiply(point, scalar: int):
    scalar = scalar % N
    if scalar == 0 or point[2] == 0:
        return (0, 0, 0)
    result = (0, 0, 0)
    addend = point
    while scalar:
        if scalar & 1:
            result = _jacobian_add(result, addend)
        addend = _jacobian_double(addend)
        scalar >>= 1
    return result


def _to_affine(point):
    x, y, z = point
    if z == 0:
        raise SigningError("point at infinity has no affine representation")
    zinv = _inv_mod(z, P)
    zinv2 = (zinv * zinv) % P
    return ((x * zinv2) % P, (y * zinv2 * zinv) % P)


def _scalar_mult(scalar: int, affine_point):
    return _to_affine(_jacobian_multiply((affine_point[0], affine_point[1], 1), scalar))


def point_is_on_curve(point) -> bool:
    """True when ``point`` satisfies the P-256 curve equation and is in range."""
    if point is None:
        return False
    x, y = point
    if not (0 <= x < P and 0 <= y < P):
        return False
    return (y * y - (x * x * x + A * x + B)) % P == 0


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------


class KeyPair:
    """A deterministic P-256 keypair usable as a fixture trust-store record."""

    __slots__ = ("private_int", "public_point")

    def __init__(self, private_int: int, public_point):
        if not 1 <= private_int < N:
            raise SigningError("private scalar out of range")
        if not point_is_on_curve(public_point):
            raise SigningError("public point is not on P-256")
        self.private_int = private_int
        self.public_point = public_point

    @property
    def public_key(self):
        return self.public_point

    def spki_der(self) -> bytes:
        return public_key_to_spki_der(self.public_point)

    def public_pem(self) -> str:
        return public_key_to_pem(self.public_point)

    def private_pem(self) -> str:
        return private_key_to_pem(self.private_int, self.public_point)

    def key_id(self) -> str:
        return derive_key_id(self.public_point)

    def sign(self, message: bytes) -> bytes:
        return sign(self.private_int, message)

    def sign_b64(self, message: bytes) -> str:
        return base64.b64encode(self.sign(message)).decode("ascii")


def generate_keypair(seed: bytes) -> KeyPair:
    """Derive a keypair deterministically from ``seed``.

    Deterministic on purpose: fixtures must be reproducible byte-for-byte
    across runs and machines, and a test that regenerates a different key on
    every invocation cannot pin an expected ``key_id`` in a receipt.
    """
    if not isinstance(seed, (bytes, bytearray)):
        raise SigningError("seed must be bytes")
    counter = 0
    while True:
        material = hashlib.sha256(b"cwk-p256-keygen-v1\x00" + bytes(seed) + counter.to_bytes(4, "big")).digest()
        candidate = int.from_bytes(material, "big")
        if 1 <= candidate < N:
            return KeyPair(candidate, _scalar_mult(candidate, (GX, GY)))
        counter += 1


def derive_key_id(public_point) -> str:
    """Stable ``key_id`` for a public key.

    Constrained by the authorization schema pattern ``^[a-z0-9][a-z0-9-]{2,63}$``,
    so this is a lowercase-hex prefix of the SPKI digest rather than free text.
    Deriving the id FROM the key means a fixture cannot quietly swap the key
    behind a stable id.
    """
    digest = hashlib.sha256(public_key_to_spki_der(public_point)).hexdigest()
    return "k-" + digest[:24]


# --------------------------------------------------------------------------
# DER primitives (strict on both write and read)
# --------------------------------------------------------------------------


def _der_length(length: int) -> bytes:
    if length < 0:
        raise SigningError("negative length")
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, "big")
    if len(body) > 4:
        raise SigningError("length too large for this profile")
    return bytes([0x80 | len(body)]) + body


def _der_tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _der_length(len(body)) + body


def _der_uint(value: int) -> bytes:
    if value < 0:
        raise SigningError("DER INTEGER must be non-negative here")
    if value == 0:
        body = b"\x00"
    else:
        body = value.to_bytes((value.bit_length() + 7) // 8, "big")
        if body[0] & 0x80:
            body = b"\x00" + body
    return _der_tlv(0x02, body)


def _der_read_tlv(buf: bytes, offset: int, expected_tag: int):
    """Read one strictly-canonical TLV, returning ``(body, next_offset)``."""
    if offset + 2 > len(buf):
        raise SigningError("truncated DER header")
    tag = buf[offset]
    if tag != expected_tag:
        raise SigningError("unexpected DER tag 0x%02x (want 0x%02x)" % (tag, expected_tag))
    first = buf[offset + 1]
    offset += 2
    if first == 0x80:
        raise SigningError("indefinite-length DER is not canonical")
    if first & 0x80:
        count = first & 0x7F
        if count > 4:
            raise SigningError("DER length octets too long")
        raw = buf[offset : offset + count]
        if len(raw) != count:
            raise SigningError("truncated DER length")
        if raw[0] == 0x00:
            raise SigningError("non-minimal DER length encoding")
        length = int.from_bytes(raw, "big")
        if length < 0x80:
            raise SigningError("long-form DER length used for a short value")
        offset += count
    else:
        length = first
    end = offset + length
    if end > len(buf):
        raise SigningError("truncated DER value")
    return buf[offset:end], end


def _der_parse_uint(body: bytes) -> int:
    if not body:
        raise SigningError("empty DER INTEGER")
    if body[0] & 0x80:
        raise SigningError("negative DER INTEGER in ECDSA-Sig-Value")
    if len(body) > 1 and body[0] == 0x00 and not (body[1] & 0x80):
        raise SigningError("non-minimal DER INTEGER (superfluous leading zero)")
    return int.from_bytes(body, "big")


def der_encode_signature(r: int, s: int) -> bytes:
    """Encode ``(r, s)`` as a canonical ECDSA-Sig-Value SEQUENCE."""
    if not 1 <= r < N or not 1 <= s < N:
        raise SigningError("signature scalar out of range")
    return _der_tlv(0x30, _der_uint(r) + _der_uint(s))


def der_decode_signature(der: bytes):
    """Strictly decode a canonical ECDSA-Sig-Value SEQUENCE into ``(r, s)``.

    Structural only: low-S is a separate policy check so that the malleability
    negative can construct a structurally perfect but high-S signature and
    still be rejected at the right layer, with the right reason.
    """
    if not isinstance(der, (bytes, bytearray)):
        raise SigningError("signature must be bytes")
    der = bytes(der)
    body, end = _der_read_tlv(der, 0, 0x30)
    if end != len(der):
        raise SigningError("trailing bytes after ECDSA-Sig-Value")
    r_body, off = _der_read_tlv(body, 0, 0x02)
    s_body, off = _der_read_tlv(body, off, 0x02)
    if off != len(body):
        raise SigningError("trailing bytes inside ECDSA-Sig-Value")
    r = _der_parse_uint(r_body)
    s = _der_parse_uint(s_body)
    if not 1 <= r < N or not 1 <= s < N:
        raise SigningError("signature scalar out of range")
    return r, s


def is_canonical_der(der: bytes) -> bool:
    try:
        der_decode_signature(der)
    except SigningError:
        return False
    return True


def is_low_s(der: bytes) -> bool:
    _, s = der_decode_signature(der)
    return s <= HALF_N


def normalize_low_s(der: bytes) -> bytes:
    """Re-encode a signature with ``s`` folded into the low half."""
    r, s = der_decode_signature(der)
    if s > HALF_N:
        s = N - s
    return der_encode_signature(r, s)


def flip_s_high(der: bytes) -> bytes:
    """Produce the malleable twin ``(r, n - s)`` of a signature.

    Only for negatives. The twin verifies mathematically, which is the point:
    a validator that checks only "does it verify" accepts two distinct
    ``signature_b64`` values, and therefore two distinct ``authorization_sha256``
    values, for one unchanged authorization body.
    """
    r, s = der_decode_signature(der)
    return der_encode_signature(r, N - s)


# --------------------------------------------------------------------------
# SPKI / PEM encoding for OpenSSL interop
# --------------------------------------------------------------------------


def _uncompressed_point(public_point) -> bytes:
    x, y = public_point
    return b"\x04" + x.to_bytes(CURVE_BYTES, "big") + y.to_bytes(CURVE_BYTES, "big")


def _parse_uncompressed_point(raw: bytes):
    if len(raw) != 1 + 2 * CURVE_BYTES or raw[0] != 0x04:
        raise SigningError("only uncompressed P-256 points are accepted")
    x = int.from_bytes(raw[1 : 1 + CURVE_BYTES], "big")
    y = int.from_bytes(raw[1 + CURVE_BYTES :], "big")
    point = (x, y)
    if not point_is_on_curve(point):
        raise SigningError("decoded public point is not on P-256")
    return point


def _ec_algorithm_identifier() -> bytes:
    return _der_tlv(0x30, _der_tlv(0x06, _OID_EC_PUBLIC_KEY) + _der_tlv(0x06, _OID_PRIME256V1))


def public_key_to_spki_der(public_point) -> bytes:
    """SubjectPublicKeyInfo DER for a P-256 public key."""
    bit_string = _der_tlv(0x03, b"\x00" + _uncompressed_point(public_point))
    return _der_tlv(0x30, _ec_algorithm_identifier() + bit_string)


def spki_der_to_public_key(spki: bytes):
    body, end = _der_read_tlv(bytes(spki), 0, 0x30)
    if end != len(spki):
        raise SigningError("trailing bytes after SubjectPublicKeyInfo")
    alg_body, off = _der_read_tlv(body, 0, 0x30)
    expected = _der_tlv(0x06, _OID_EC_PUBLIC_KEY) + _der_tlv(0x06, _OID_PRIME256V1)
    if alg_body != expected:
        raise SigningError("SubjectPublicKeyInfo is not id-ecPublicKey/prime256v1")
    bits, off = _der_read_tlv(body, off, 0x03)
    if off != len(body):
        raise SigningError("trailing bytes inside SubjectPublicKeyInfo")
    if not bits or bits[0] != 0x00:
        raise SigningError("unexpected unused bits in public key BIT STRING")
    return _parse_uncompressed_point(bits[1:])


def _pem_wrap(label: str, der: bytes) -> str:
    encoded = base64.b64encode(der).decode("ascii")
    lines = [encoded[i : i + 64] for i in range(0, len(encoded), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, "\n".join(lines), label)


def _pem_unwrap(pem: str, label: str) -> bytes:
    begin = "-----BEGIN %s-----" % label
    end = "-----END %s-----" % label
    if begin not in pem or end not in pem:
        raise SigningError("PEM label %s not found" % label)
    inner = pem.split(begin, 1)[1].split(end, 1)[0]
    return base64.b64decode("".join(inner.split()))


def public_key_to_pem(public_point) -> str:
    return _pem_wrap("PUBLIC KEY", public_key_to_spki_der(public_point))


def pem_to_public_key(pem: str):
    return spki_der_to_public_key(_pem_unwrap(pem, "PUBLIC KEY"))


def private_key_to_pem(private_int: int, public_point) -> str:
    """PKCS#8 PEM, which is what LibreSSL's ``dgst -sign`` will read."""
    ec_private_key = _der_tlv(
        0x30,
        _der_uint(1)
        + _der_tlv(0x04, private_int.to_bytes(CURVE_BYTES, "big"))
        + _der_tlv(0xA1, _der_tlv(0x03, b"\x00" + _uncompressed_point(public_point))),
    )
    pkcs8 = _der_tlv(
        0x30,
        _der_uint(0) + _ec_algorithm_identifier() + _der_tlv(0x04, ec_private_key),
    )
    return _pem_wrap("PRIVATE KEY", pkcs8)


# --------------------------------------------------------------------------
# RFC 6979 deterministic nonce
# --------------------------------------------------------------------------


def _bits2int(data: bytes, qlen: int) -> int:
    value = int.from_bytes(data, "big")
    excess = len(data) * 8 - qlen
    if excess > 0:
        value >>= excess
    return value


def _int2octets(value: int, rolen: int) -> bytes:
    return value.to_bytes(rolen, "big")


def _bits2octets(data: bytes, qlen: int, rolen: int) -> bytes:
    z1 = _bits2int(data, qlen)
    z2 = z1 - N
    if z2 < 0:
        z2 = z1
    return _int2octets(z2, rolen)


def _rfc6979_nonce(private_int: int, digest: bytes) -> int:
    """Deterministic ``k`` per RFC 6979 with HMAC-SHA256.

    Deterministic nonces are not a convenience here. ECDSA leaks the private
    key outright if ``k`` ever repeats across two signatures, and a fixture
    generator that reaches for a weak PRNG would do exactly that. RFC 6979
    removes the entropy source from the threat model entirely and makes every
    fixture signature byte-reproducible.
    """
    qlen = N.bit_length()
    rolen = (qlen + 7) // 8
    holen = hashlib.sha256().digest_size
    prefix = _int2octets(private_int, rolen) + _bits2octets(digest, qlen, rolen)

    v = b"\x01" * holen
    k = b"\x00" * holen
    k = hmac.new(k, v + b"\x00" + prefix, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + prefix, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()

    while True:
        t = b""
        while len(t) < rolen:
            v = hmac.new(k, v, hashlib.sha256).digest()
            t += v
        candidate = _bits2int(t, qlen)
        if 1 <= candidate < N:
            return candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


# --------------------------------------------------------------------------
# Sign / verify
# --------------------------------------------------------------------------


def sign(private_int: int, message: bytes) -> bytes:
    """Sign ``message`` (hashed with SHA-256), returning canonical low-S DER."""
    if not 1 <= private_int < N:
        raise SigningError("private scalar out of range")
    digest = hashlib.sha256(message).digest()
    z = _bits2int(digest, N.bit_length())
    while True:
        k = _rfc6979_nonce(private_int, digest)
        point = _scalar_mult(k, (GX, GY))
        r = point[0] % N
        if r == 0:
            continue
        s = (_inv_mod(k, N) * (z + r * private_int)) % N
        if s == 0:
            continue
        if s > HALF_N:
            s = N - s
        return der_encode_signature(r, s)


def verify(public_point, message: bytes, der_signature: bytes, *, require_low_s: bool = True) -> bool:
    """Verify a detached signature. Returns False rather than raising on bad input.

    ``require_low_s`` defaults to True: a high-S signature is a *valid* ECDSA
    signature and will pass the mathematics, so refusing it is a policy the
    verifier must apply deliberately. The flag exists so the malleability
    negative can demonstrate that the twin really does verify mathematically
    and is rejected only because of this policy.
    """
    try:
        r, s = der_decode_signature(der_signature)
    except SigningError:
        return False
    if require_low_s and s > HALF_N:
        return False
    if not point_is_on_curve(public_point):
        return False
    digest = hashlib.sha256(message).digest()
    z = _bits2int(digest, N.bit_length())
    try:
        w = _inv_mod(s, N)
    except SigningError:
        return False
    u1 = (z * w) % N
    u2 = (r * w) % N
    point = _jacobian_add(
        _jacobian_multiply((GX, GY, 1), u1),
        _jacobian_multiply((public_point[0], public_point[1], 1), u2),
    )
    if point[2] == 0:
        return False
    x, _ = _to_affine(point)
    return (x % N) == (r % N)


def verify_b64(public_point, message: bytes, signature_b64: str, *, require_low_s: bool = True) -> bool:
    """Verify a base64-wrapped signature as it appears in the receipt body."""
    try:
        raw = base64.b64decode(signature_b64, validate=True)
    except Exception:
        return False
    # Round-trip guard: the receipt stores base64, and a non-canonical base64
    # encoding is another way to get two spellings of one signature.
    if base64.b64encode(raw).decode("ascii") != signature_b64:
        return False
    return verify(public_point, message, raw, require_low_s=require_low_s)


# --------------------------------------------------------------------------
# OpenSSL interop (fail closed, never skip)
# --------------------------------------------------------------------------


def openssl_path() -> str:
    """Locate the OpenSSL/LibreSSL binary or raise :class:`SigningDependencyError`."""
    found = shutil.which("openssl") or ("/usr/bin/openssl" if os.path.exists("/usr/bin/openssl") else None)
    if not found:
        raise SigningDependencyError(
            "openssl binary not found; refusing to report signature verification "
            "as successful without it. Install openssl/libressl or run the suite "
            "on the reference host."
        )
    return found


def require_openssl() -> str:
    """Assert interop is genuinely available, including P-256 support."""
    binary = openssl_path()
    try:
        proc = subprocess.run(
            [binary, "ecparam", "-list_curves"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - host defect
        raise SigningDependencyError("openssl is present but unusable: %s" % (exc,)) from exc
    if proc.returncode != 0 or "prime256v1" not in proc.stdout:
        raise SigningDependencyError(
            "openssl at %s does not advertise prime256v1; ecdsa-p256-sha256 cannot "
            "be independently verified on this host." % binary
        )
    return binary


def openssl_verify(public_pem: str, message: bytes, der_signature: bytes) -> bool:
    """Independently verify with OpenSSL.

    A second implementation matters because the in-process verifier and the
    in-process signer share this file: if both were wrong in the same way the
    tests would still be green. OpenSSL has no such shared bug.
    """
    binary = require_openssl()
    with tempfile.TemporaryDirectory() as tmp:
        key_path = os.path.join(tmp, "pub.pem")
        msg_path = os.path.join(tmp, "message.bin")
        sig_path = os.path.join(tmp, "sig.der")
        with open(key_path, "w", encoding="utf-8") as handle:
            handle.write(public_pem)
        with open(msg_path, "wb") as handle:
            handle.write(message)
        with open(sig_path, "wb") as handle:
            handle.write(der_signature)
        proc = subprocess.run(
            [binary, "dgst", "-sha256", "-verify", key_path, "-signature", sig_path, msg_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
    return proc.returncode == 0 and "Verified OK" in proc.stdout


def openssl_sign(private_pem: str, message: bytes) -> bytes:
    """Sign with OpenSSL, for the reverse interop direction.

    The result is NOT normalised here: OpenSSL emits whatever ``s`` it computed,
    so roughly half of these come back high-S. That is deliberate - it gives the
    malleability negative a signature produced by a real, correct third-party
    signer rather than one the test manufactured.
    """
    binary = require_openssl()
    with tempfile.TemporaryDirectory() as tmp:
        key_path = os.path.join(tmp, "priv.pem")
        msg_path = os.path.join(tmp, "message.bin")
        sig_path = os.path.join(tmp, "sig.der")
        with open(key_path, "w", encoding="utf-8") as handle:
            handle.write(private_pem)
        with open(msg_path, "wb") as handle:
            handle.write(message)
        proc = subprocess.run(
            [binary, "dgst", "-sha256", "-sign", key_path, "-out", sig_path, msg_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise SigningDependencyError("openssl failed to sign: %s" % (proc.stderr.strip(),))
        with open(sig_path, "rb") as handle:
            return handle.read()
