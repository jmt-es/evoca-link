import base64
import hashlib
import hmac
import json
import struct
import unicodedata
from dataclasses import dataclass

PAYLOAD_PREFIX = "EVOCALINK1:"
PAYLOAD_VERSION = 1
PAIR_ALPN = b"evoca/pair/1"
LINK_ALPN = b"evoca/link/1"
RPC_VERSION = 1
KEY_BYTES = 32
MAX_NAME_BYTES = 63
MAX_RELAY_BYTES = 200
MAX_ADDRESSES = 6
MAX_ADDRESS_BYTES = 64
MAX_FRAME_BYTES = 16 * 1024 * 1024
FRAME_HEADER_BYTES = 4
OS_CODES = {"other": 0, "macos": 1, "windows": 2, "linux": 3}
OS_NAMES = {code: name for name, code in OS_CODES.items()}

SAFETY_LABEL = b"evoca-link/safety/v1"
PROOF_LABEL = b"evoca-link/pair/v1"
CONFIRMATION_LABEL = b"evoca-link/paired/v1"
SAFETY_DIGITS = 1_000_000


class PayloadError(ValueError):
    pass


class FrameError(ValueError):
    pass


class RemoteError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def truncated_utf8(text: str, limit: int) -> bytes:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return encoded
    return encoded[:limit].decode("utf-8", errors="ignore").encode("utf-8")


def clean_name(text: str) -> str:
    visible = "".join(c for c in text if unicodedata.category(c) != "Cc").strip()
    return truncated_utf8(visible, MAX_NAME_BYTES).decode("utf-8").strip()


def _valid_name(name_bytes: bytes) -> bool:
    try:
        name = name_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return 0 < len(name_bytes) <= MAX_NAME_BYTES and not any(unicodedata.category(c) == "Cc" for c in name)


def _ascii_word(text: str) -> bool:
    return text.isascii() and all(0x20 < ord(c) < 0x7F for c in text)


def valid_relay(url: str) -> bool:
    return _ascii_word(url) and len(url) <= MAX_RELAY_BYTES and url.startswith(("https://", "http://"))


def valid_address(address: str) -> bool:
    if not _ascii_word(address) or len(address) > MAX_ADDRESS_BYTES:
        return False
    host, separator, port = address.rpartition(":")
    if not separator or not host or not port.isdigit() or len(port) > 5:
        return False
    return 0 < int(port) < 65536 and all(c.isalnum() or c in ".:[]%" for c in host)


@dataclass(frozen=True)
class PairingOffer:
    computer_id: bytes
    secret: bytes
    expires_at: int
    name: str
    os: str = "other"
    relay_url: str | None = None
    addresses: tuple[str, ...] = ()

    def encode(self) -> str:
        if len(self.computer_id) != KEY_BYTES or len(self.secret) != KEY_BYTES:
            raise PayloadError("computer_id and secret must be 32 bytes")
        name = clean_name(self.name).encode("utf-8")
        if not name:
            raise PayloadError("the computer needs a name")
        if self.relay_url is not None and not valid_relay(self.relay_url):
            raise PayloadError("invalid relay URL")
        relay = (self.relay_url or "").encode("ascii")
        addresses = [a.encode("ascii") for a in self.addresses if valid_address(a)][:MAX_ADDRESSES]
        body = bytearray([PAYLOAD_VERSION])
        body += self.computer_id + self.secret
        body += struct.pack(">I", self.expires_at)
        body.append(OS_CODES.get(self.os, 0))
        body.append(len(name))
        body += name
        body.append(len(relay))
        body += relay
        body.append(len(addresses))
        for address in addresses:
            body.append(len(address))
            body += address
        return PAYLOAD_PREFIX + base64.b32encode(bytes(body)).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, text: str) -> "PairingOffer":
        compact = "".join(text.split())
        if not compact.upper().startswith(PAYLOAD_PREFIX):
            raise PayloadError("not an Evoca Link code")
        encoded = compact[len(PAYLOAD_PREFIX):].upper()
        try:
            body = base64.b32decode(encoded + "=" * (-len(encoded) % 8))
        except ValueError as error:
            raise PayloadError("the code is damaged") from error
        reader = _Reader(body)
        if reader.byte() != PAYLOAD_VERSION:
            raise PayloadError("this code comes from a newer Evoca Link")
        computer_id = reader.take(KEY_BYTES)
        secret = reader.take(KEY_BYTES)
        expires_at = struct.unpack(">I", reader.take(4))[0]
        os_name = OS_NAMES.get(reader.byte(), "other")
        name_bytes = reader.take(reader.byte())
        relay_bytes = reader.take(reader.byte())
        count = reader.byte()
        if count > MAX_ADDRESSES:
            raise PayloadError("the code is damaged")
        address_bytes = [reader.take(reader.byte()) for _ in range(count)]
        if not reader.done or not _valid_name(name_bytes):
            raise PayloadError("the code is damaged")
        name = name_bytes.decode("utf-8")
        try:
            relay = relay_bytes.decode("ascii") or None
            addresses = tuple(raw.decode("ascii") for raw in address_bytes)
        except UnicodeDecodeError as error:
            raise PayloadError("the code is damaged") from error
        if relay is not None and not valid_relay(relay):
            raise PayloadError("the code is damaged")
        if not all(valid_address(a) for a in addresses):
            raise PayloadError("the code is damaged")
        return cls(computer_id=computer_id, secret=secret, expires_at=expires_at, name=name, os=os_name, relay_url=relay, addresses=addresses)

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def safety_code(self) -> str:
        return safety_code(self.secret, self.computer_id)


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def take(self, count: int) -> bytes:
        if self.offset + count > len(self.data):
            raise PayloadError("the code is damaged")
        chunk = self.data[self.offset:self.offset + count]
        self.offset += count
        return chunk

    def byte(self) -> int:
        return self.take(1)[0]

    @property
    def done(self) -> bool:
        return self.offset == len(self.data)


def _mac(secret: bytes, label: bytes, *parts: bytes) -> bytes:
    return hmac.new(secret, label + b"\x00" + b"".join(parts), hashlib.sha256).digest()


def safety_code(secret: bytes, computer_id: bytes) -> str:
    digest = _mac(secret, SAFETY_LABEL, computer_id)
    return f"{struct.unpack('>I', digest[:4])[0] % SAFETY_DIGITS:06d}"


def display_code(code: str) -> str:
    return f"{code[:3]} {code[3:]}"


def pairing_proof(secret: bytes, computer_id: bytes, phone_id: bytes, phone_name: str) -> bytes:
    name = truncated_utf8(phone_name, MAX_NAME_BYTES)
    return _mac(secret, PROOF_LABEL, computer_id, phone_id, bytes([len(name)]), name)


def pairing_confirmation(secret: bytes, computer_id: bytes, phone_id: bytes) -> bytes:
    return _mac(secret, CONFIRMATION_LABEL, computer_id, phone_id)


def proofs_match(expected: bytes, received: bytes) -> bool:
    return hmac.compare_digest(expected, received)


def encode_frame(message: dict) -> bytes:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise FrameError(f"frame of {len(body)} bytes is over the {MAX_FRAME_BYTES} limit")
    return struct.pack(">I", len(body)) + body


class FrameReader:
    def __init__(self, limit: int = MAX_FRAME_BYTES):
        self.limit = limit
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[dict]:
        self.buffer += data
        frames = []
        while len(self.buffer) >= FRAME_HEADER_BYTES:
            length = struct.unpack(">I", self.buffer[:FRAME_HEADER_BYTES])[0]
            if length > self.limit:
                raise FrameError(f"frame of {length} bytes is over the {self.limit} limit")
            if len(self.buffer) < FRAME_HEADER_BYTES + length:
                break
            body = bytes(self.buffer[FRAME_HEADER_BYTES:FRAME_HEADER_BYTES + length])
            del self.buffer[:FRAME_HEADER_BYTES + length]
            try:
                message = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise FrameError("frame is not UTF-8 JSON") from error
            if not isinstance(message, dict):
                raise FrameError("frame is not a JSON object")
            frames.append(message)
        return frames

    @property
    def pending(self) -> int:
        return len(self.buffer)


def rpc_request(request_id: int, method: str, params: dict) -> dict:
    return {"v": RPC_VERSION, "id": request_id, "method": method, "params": params}


def rpc_result(message: dict, request_id: int) -> tuple[dict, str | None]:
    if message.get("id") != request_id:
        raise FrameError("response for another request")
    if message.get("ok") is True:
        result = message.get("result")
        if not isinstance(result, dict):
            raise FrameError("response without a result object")
        return result, message.get("as_of")
    error = message.get("error") or {}
    raise RemoteError(str(error.get("code", "internal")), str(error.get("message", "the iPhone could not answer")))


def pair_request(phone_name: str, proof: bytes, app: str) -> dict:
    return {"type": "pair", "v": RPC_VERSION, "phone_name": phone_name, "proof": base64.b64encode(proof).decode("ascii"), "app": app}


def decode_b64(value: object, length: int) -> bytes:
    if not isinstance(value, str):
        raise FrameError("expected base64 text")
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise FrameError("invalid base64") from error
    if len(raw) != length:
        raise FrameError(f"expected {length} bytes")
    return raw
