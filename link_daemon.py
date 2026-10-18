import asyncio
import base64
import json
import logging
import logging.handlers
import os
import secrets
import time
from dataclasses import dataclass, field

import iroh

from link_protocol import (
    LINK_ALPN,
    MAX_FRAME_BYTES,
    PAIR_ALPN,
    RPC_VERSION,
    FrameError,
    FrameReader,
    PairingOffer,
    RemoteError,
    decode_b64,
    encode_frame,
    pairing_confirmation,
    pairing_proof,
    proofs_match,
    rpc_request,
    rpc_result,
)
from link_state import DaemonInfo, LinkState, SingleInstance, computer_name, os_name

log = logging.getLogger("evoca-link")

OFFER_SECONDS = 300
CROWD_GRACE_SECONDS = 300
HANDSHAKE_SECONDS = 20
CALL_SECONDS = 15
PROBE_SECONDS = 3
PROBE_METHOD = "ping"
ONLINE_SECONDS = 8
CONTROL_LINE_LIMIT = 1024 * 1024
MAX_PARAMS_BYTES = 64 * 1024
LOG_BYTES = 1_000_000
MAX_ADDRESSES_IN_QR = 4
CLOSE_REVOKED = 2
CLOSE_NOT_PAIRED = 3
CLOSE_REPLACED = 4
CLOSE_SHUTDOWN = 5


class ControlError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def read_frame(stream, timeout: float) -> dict:
    data = await asyncio.wait_for(stream.read_to_end(MAX_FRAME_BYTES + 4), timeout)
    frames = FrameReader().feed(data)
    if len(frames) != 1:
        raise FrameError("expected exactly one frame")
    return frames[0]


async def send_frame(stream, message: dict) -> None:
    await stream.write_all(encode_frame(message))
    await stream.finish()


@dataclass
class PendingOffer:
    offer: PairingOffer
    paired: asyncio.Event = field(default_factory=asyncio.Event)
    phone: dict | None = None
    burned: bool = False

    @property
    def expired(self) -> bool:
        return self.offer.is_expired(time.time())


@dataclass
class PhoneSession:
    connection: object
    phone_id: str
    name: str
    device: str
    app: str
    time_zone: str | None
    connected_at: float
    next_id: int = 1

    def path(self) -> dict:
        try:
            selected = next((p for p in self.connection.paths() if p.is_selected), None)
        except Exception:
            selected = None
        if selected is None:
            return {"kind": "unknown"}
        return {"kind": "relay" if selected.is_relay else "direct", "rtt_ms": selected.rtt_ms}

    async def call(self, method: str, params: dict, timeout: float) -> tuple[dict, str | None]:
        request_id = self.next_id
        self.next_id += 1
        return await asyncio.wait_for(self.exchange(request_id, method, params, timeout), timeout)

    async def answers_probe(self) -> bool:
        """Any reply, even an error from an iPhone without the probe, proves Evoca is still open and only the late question is slow."""
        try:
            await self.call(PROBE_METHOD, {}, PROBE_SECONDS)
        except RemoteError:
            return True
        except (asyncio.TimeoutError, iroh.IrohError, FrameError):
            return False
        return True

    async def exchange(self, request_id: int, method: str, params: dict, timeout: float) -> tuple[dict, str | None]:
        stream = await self.connection.open_bi()
        await send_frame(stream.send(), rpc_request(request_id, method, params))
        message = await read_frame(stream.recv(), timeout)
        return rpc_result(message, request_id)


class LinkDaemon:
    def __init__(self, state: LinkState, name: str | None = None, relay: bool = True, bind_addr: str | None = None):
        self.state = state
        self.name = name or computer_name()
        self.relay = relay
        self.bind_addr = bind_addr
        self.endpoint = None
        self.sessions: dict[str, PhoneSession] = {}
        self.offer: PendingOffer | None = None
        self.claimed: PendingOffer | None = None
        self.server: asyncio.base_events.Server | None = None
        self.token = secrets.token_hex(32)
        self.stopping = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()

    def spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.finished)

    def finished(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed: %r", task.exception(), exc_info=task.exception())

    @property
    def computer_id(self) -> bytes:
        return self.endpoint.id().to_bytes()

    async def start(self) -> None:
        options = iroh.EndpointOptions(
            preset=iroh.preset_n0() if self.relay else iroh.preset_minimal(),
            secret_key=self.state.secret_key(),
            alpns=[PAIR_ALPN, LINK_ALPN],
        )
        if self.bind_addr:
            options.bind_addr = self.bind_addr
        self.endpoint = await iroh.Endpoint.bind(options)
        self.server = await asyncio.start_server(self.serve_control, "127.0.0.1", 0, limit=CONTROL_LINE_LIMIT)
        port = self.server.sockets[0].getsockname()[1]
        self.state.write_daemon_info(DaemonInfo(port=port, token=self.token, pid=os.getpid(), started_at=time.time()))
        self.spawn(self.accept_loop())
        log.info("listening as %s (%s) on control port %d", self.endpoint.id().fmt_short(), self.name, port)

    async def run(self) -> None:
        await self.start()
        try:
            await self.stopping.wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            self.close_session(session, CLOSE_SHUTDOWN, b"computer shutting down")
        if self.server:
            self.server.close()
        if self.endpoint:
            try:
                await asyncio.wait_for(self.endpoint.close(), 5)
            except (asyncio.TimeoutError, iroh.IrohError):
                pass
        self.state.clear_daemon_info(os.getpid())

    def close_session(self, session: PhoneSession, code: int, reason: bytes) -> None:
        try:
            session.connection.close(code, reason)
        except iroh.IrohError:
            pass
        if self.sessions.get(session.phone_id) is session:
            del self.sessions[session.phone_id]

    async def accept_loop(self) -> None:
        while not self.stopping.is_set():
            incoming = await self.endpoint.accept_next()
            if incoming is None:
                return
            self.spawn(self.handle_incoming(incoming))

    async def handle_incoming(self, incoming) -> None:
        try:
            accepting = await incoming.accept()
            alpn = await accepting.alpn()
            connection = await accepting.connect()
        except iroh.IrohError as error:
            log.info("incoming connection failed: %s", error.message())
            return
        try:
            if alpn == PAIR_ALPN:
                await self.handle_pairing(connection)
            elif alpn == LINK_ALPN:
                await self.handle_link(connection)
        except (asyncio.TimeoutError, FrameError, iroh.IrohError, ValueError) as error:
            log.info("connection from %s ended: %s", connection.remote_id().fmt_short(), type(error).__name__)
            self.drop(connection, b"protocol error")
        except Exception:
            log.exception("connection from %s failed", connection.remote_id().fmt_short())
            self.drop(connection, b"computer error")

    @staticmethod
    def drop(connection, reason: bytes) -> None:
        try:
            connection.close(1, reason)
        except iroh.IrohError:
            pass

    async def handle_pairing(self, connection) -> None:
        phone_id = connection.remote_id().to_bytes()
        stream = await asyncio.wait_for(connection.accept_bi(), HANDSHAKE_SECONDS)
        message = await read_frame(stream.recv(), HANDSHAKE_SECONDS)
        pending, reason = self.offer_proved_by(phone_id, message)
        if pending is not None:
            reason = self.claim(pending, phone_id, message)
        if reason:
            log.info("pairing refused: %s", reason)
            await send_frame(stream.send(), {"type": "pair_rejected", "v": RPC_VERSION, "reason": reason})
            await asyncio.sleep(0.5)
            connection.close(1, reason.encode())
            return
        confirmation = pairing_confirmation(pending.offer.secret, self.computer_id, phone_id)
        await send_frame(stream.send(), {
            "type": "paired",
            "v": RPC_VERSION,
            "computer_name": self.name,
            "confirmation": base64.b64encode(confirmation).decode("ascii"),
        })
        log.info("paired with %s (%s)", pending.phone["name"], pending.phone["id"][:10])
        try:
            await asyncio.wait_for(connection.closed(), 10)
        except asyncio.TimeoutError:
            connection.close(0, b"paired")

    async def handle_link(self, connection) -> None:
        phone_id = connection.remote_id().to_bytes().hex()
        stream = await asyncio.wait_for(connection.accept_bi(), HANDSHAKE_SECONDS)
        hello = await read_frame(stream.recv(), HANDSHAKE_SECONDS)
        phone = self.state.phone(phone_id)
        if hello.get("type") != "hello" or phone is None:
            await send_frame(stream.send(), {"type": "denied", "v": RPC_VERSION, "reason": "not_paired" if phone is None else "bad_request"})
            await asyncio.sleep(0.5)
            connection.close(CLOSE_NOT_PAIRED, b"not paired")
            log.info("refused a link from an unpaired iPhone %s", phone_id[:10])
            return
        address = self.endpoint.addr()
        await send_frame(stream.send(), {
            "type": "welcome",
            "v": RPC_VERSION,
            "computer": self.name,
            "relay_url": address.relay_url(),
            "addresses": address.direct_addresses()[:MAX_ADDRESSES_IN_QR],
        })
        previous = self.sessions.get(phone_id)
        session = PhoneSession(
            connection=connection,
            phone_id=phone_id,
            name=phone.name,
            device=str(hello.get("device") or phone.name),
            app=str(hello.get("app") or ""),
            time_zone=hello.get("time_zone"),
            connected_at=time.time(),
        )
        self.sessions[phone_id] = session
        try:
            if previous:
                self.close_session(previous, CLOSE_REPLACED, b"replaced by a newer link")
            self.seen(phone_id, session.app)
            log.info("live link with %s over %s", phone.name, session.path().get("kind"))
            self.spawn(self.phone_messages(session))
            reason = await connection.closed()
            log.info("link with %s closed: %s", phone.name, reason)
        finally:
            if self.sessions.get(phone_id) is session:
                del self.sessions[phone_id]
                self.seen(phone_id, session.app)

    def seen(self, phone_id: str, app: str) -> None:
        try:
            self.state.touch_phone(phone_id, app)
        except (OSError, RuntimeError) as error:
            log.warning("could not note when the iPhone was last seen: %s", error)

    async def phone_messages(self, session: PhoneSession) -> None:
        while self.sessions.get(session.phone_id) is session:
            try:
                stream = await session.connection.accept_bi()
                message = await read_frame(stream.recv(), HANDSHAKE_SECONDS)
            except (iroh.IrohError, asyncio.TimeoutError, FrameError):
                return
            if message.get("type") == "revoke":
                self.state.remove_phone(session.phone_id)
                log.info("%s disconnected this computer", session.name)
                await send_frame(stream.send(), {"type": "revoked", "v": RPC_VERSION})
                await asyncio.sleep(0.3)
                self.close_session(session, CLOSE_REVOKED, b"revoked")
                return
            await send_frame(stream.send(), {"type": "ignored", "v": RPC_VERSION})

    def offer_proved_by(self, phone_id: bytes, message: dict) -> tuple[PendingOffer | None, str | None]:
        if message.get("type") != "pair" or not isinstance(message.get("phone_name"), str):
            return None, "bad_request"
        proof = decode_b64(message.get("proof"), 32)
        if self.claimed is not None and time.time() > self.claimed.offer.expires_at + CROWD_GRACE_SECONDS:
            # Evoca refuses expired codes itself, so a proof long after expiry only comes from an old screenshot: it must not unpair the iPhone.
            self.claimed = None
        candidates = [pending for pending in (self.offer, self.claimed) if pending is not None]
        for pending in candidates:
            expected = pairing_proof(pending.offer.secret, self.computer_id, phone_id, message["phone_name"])
            if proofs_match(expected, proof):
                return pending, None
        return None, "invalid_proof" if candidates else "no_offer"

    def claim(self, pending: PendingOffer, phone_id: bytes, message: dict) -> str | None:
        """A second iPhone proving the same one-time secret means someone else saw the QR, so neither keeps the pairing (a crowded mailbox in Magic Wormhole terms)."""
        phone_hex = phone_id.hex()
        if pending.phone is not None and pending.phone["id"] != phone_hex:
            self.burn(pending)
            return "crowded"
        if pending.burned:
            return "crowded"
        if pending.expired:
            return "expired"
        phone = self.state.add_phone(phone_hex, message["phone_name"], message.get("app"))
        if pending.phone is None:
            pending.phone = {"id": phone.id, "name": phone.name}
            pending.paired.set()
        return None

    def burn(self, pending: PendingOffer) -> None:
        pending.burned = True
        first = pending.phone["id"]
        self.state.remove_phone(first)
        session = self.sessions.get(first)
        if session:
            self.close_session(session, CLOSE_NOT_PAIRED, b"pairing code used by another device")
        log.warning("a second device used the pairing code that paired %s: both refused, pair again with a new code", pending.phone["name"])

    def current_session(self) -> PhoneSession | None:
        return max(self.sessions.values(), key=lambda s: s.connected_at, default=None)

    async def create_offer(self, ttl: int) -> dict:
        if self.relay:
            try:
                await asyncio.wait_for(self.endpoint.online(), ONLINE_SECONDS)
            except asyncio.TimeoutError:
                log.info("no relay reachable; the QR only carries direct addresses")
        address = self.endpoint.addr()
        offer = PairingOffer(
            computer_id=self.computer_id,
            secret=secrets.token_bytes(32),
            expires_at=int(time.time()) + max(30, min(ttl, 900)),
            name=self.name,
            os=os_name(),
            relay_url=address.relay_url() if self.relay else None,
            addresses=tuple(address.direct_addresses()[:MAX_ADDRESSES_IN_QR]),
        )
        if self.offer is not None and self.offer.phone is not None:
            self.claimed = self.offer
        self.offer = PendingOffer(offer=offer)
        return {"text": offer.encode(), "safety_code": offer.safety_code(), "expires_at": offer.expires_at, "computer": self.name}

    async def wait_for_pairing(self, timeout: float) -> dict:
        pending = self.offer
        if pending is None:
            raise ControlError("no_offer", "There is no pairing code waiting")
        remaining = max(0.0, min(timeout, pending.offer.expires_at - time.time()))
        try:
            await asyncio.wait_for(pending.paired.wait(), remaining)
        except asyncio.TimeoutError:
            if pending.expired:
                if self.offer is pending:
                    self.offer = None
                raise ControlError("expired", "The pairing code expired") from None
            raise ControlError("timeout", "Still waiting for the iPhone") from None
        if self.offer is pending:
            self.offer = None
        self.claimed = pending
        if pending.burned:
            raise ControlError("crowded", "Another device used the same code, so neither is paired")
        return pending.phone or {}

    def status(self) -> dict:
        address = self.endpoint.addr()
        phones = []
        for phone in self.state.phones():
            session = self.sessions.get(phone.id)
            phones.append({
                "id": phone.id,
                "name": phone.name,
                "paired_at": phone.paired_at,
                "last_seen": time.time() if session else phone.last_seen,
                "connected": session is not None,
                "connected_since": session.connected_at if session else None,
                "device": session.device if session else None,
                "app": session.app if session else phone.app,
                "path": session.path() if session else None,
            })
        return {
            "computer": {"name": self.name, "id": self.endpoint.id().to_bytes().hex(), "relay_url": address.relay_url(), "addresses": address.direct_addresses()},
            "phones": phones,
            "waiting_for_pairing": self.offer is not None and not self.offer.expired and self.offer.phone is None,
        }

    async def call(self, method: str, params: dict, timeout: float) -> dict:
        session = self.current_session()
        if session is None:
            raise ControlError("not_connected", "not connected")
        started = time.monotonic()
        try:
            result, as_of = await session.call(method, params, timeout)
        except RemoteError as error:
            raise ControlError(f"remote:{error.code}", error.message) from error
        except asyncio.TimeoutError:
            if await session.answers_probe():
                raise ControlError("slow", f"The iPhone is connected but did not finish {method} in {timeout:.0f} s") from None
            self.close_session(session, 1, b"no answer")
            raise ControlError("timeout", "The iPhone stopped answering") from None
        except iroh.IrohError as error:
            self.close_session(session, 1, b"stream failed")
            raise ControlError("not_connected", f"the link dropped: {error.message()}") from error
        log.info("%s answered in %.2fs", method, time.monotonic() - started)
        return {"result": result, "as_of": as_of, "phone": session.name, "path": session.path()["kind"]}

    def unpair(self, wanted: str) -> dict:
        phone = self.state.find_phone(wanted)
        if phone is None:
            raise ControlError("not_found", f"No paired iPhone matches «{wanted}»")
        self.state.remove_phone(phone.id)
        if self.offer is not None and self.offer.phone is not None and self.offer.phone["id"] == phone.id:
            self.offer = None
        if self.claimed is not None and self.claimed.phone is not None and self.claimed.phone["id"] == phone.id:
            self.claimed = None
        session = self.sessions.get(phone.id)
        if session:
            self.close_session(session, CLOSE_NOT_PAIRED, b"unpaired on the computer")
        return {"id": phone.id, "name": phone.name}

    async def serve_control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict) or not secrets.compare_digest(str(request.get("token", "")), self.token):
                raise ControlError("unauthorized", "Wrong control token")
            reply = {"ok": True, **(await self.control(request))}
        except ControlError as error:
            reply = {"ok": False, "error": {"code": error.code, "message": error.message}}
        except (ValueError, asyncio.TimeoutError, asyncio.LimitOverrunError):
            reply = {"ok": False, "error": {"code": "bad_request", "message": "Malformed control request"}}
        except Exception as error:
            log.exception("control request failed")
            reply = {"ok": False, "error": {"code": "internal", "message": f"{type(error).__name__}: {error}"}}
        try:
            writer.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def control(self, request: dict) -> dict:
        operation = request.get("op")
        if operation == "ping":
            return {"pid": os.getpid()}
        if operation == "status":
            return self.status()
        if operation == "call":
            params = request.get("params") or {}
            if not isinstance(params, dict) or not isinstance(request.get("method"), str):
                raise ControlError("bad_request", "call needs a method and params")
            if len(json.dumps(params, ensure_ascii=False).encode("utf-8")) > MAX_PARAMS_BYTES:
                raise ControlError("bad_request", f"The request is over {MAX_PARAMS_BYTES // 1024} KB; the iPhone only reads short questions")
            timeout = float(request.get("timeout") or CALL_SECONDS)
            return await self.call(request["method"], params, min(timeout, 120))
        if operation == "pair":
            return await self.create_offer(int(request.get("ttl") or OFFER_SECONDS))
        if operation == "pair_wait":
            return {"phone": await self.wait_for_pairing(float(request.get("timeout") or 60))}
        if operation == "pair_cancel":
            self.offer = None
            return {}
        if operation == "unpair":
            return {"removed": self.unpair(str(request.get("phone") or ""))}
        if operation == "stop":
            self.stopping.set()
            return {}
        raise ControlError("bad_request", f"Unknown operation {operation!r}")


def configure_logging(state: LinkState, to_stderr: bool) -> None:
    state.ensure()
    handlers: list[logging.Handler] = [logging.StreamHandler()] if to_stderr else []
    handlers.append(logging.handlers.RotatingFileHandler(state.log_path, maxBytes=LOG_BYTES, backupCount=1, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers, force=True)


async def run_daemon(state: LinkState, name: str | None = None, relay: bool = True, bind_addr: str | None = None) -> int:
    instance = SingleInstance(state.lock_path)
    if not instance.acquire():
        log.info("another evoca-link daemon is already running")
        return 0
    daemon = LinkDaemon(state, name=name, relay=relay, bind_addr=bind_addr)
    await daemon.run()
    return 0
