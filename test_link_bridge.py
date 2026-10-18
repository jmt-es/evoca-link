import asyncio
import base64
import dataclasses
import json
import logging
import tempfile
import unittest
from pathlib import Path

try:
    import iroh
except ImportError:
    iroh = None

import link_protocol as lp
from link_state import LinkState

if iroh is not None:
    import link_daemon
    from link_client import ControlFailure, request
    from link_daemon import ControlError, LinkDaemon, configure_logging, read_frame, send_frame

AS_OF = "2026-10-10T21:00:00+02:00"


class FakePhone:
    def __init__(self, name: str = "iPhone de prueba"):
        self.name = name
        self.endpoint = None
        self.connection = None
        self.served: list[dict] = []
        self.server = None
        self.frozen = False
        self.answering: set[asyncio.Task] = set()

    async def start(self):
        self.endpoint = await iroh.Endpoint.bind(iroh.EndpointOptions(preset=iroh.preset_minimal(), bind_addr="127.0.0.1:0"))

    @property
    def id(self) -> bytes:
        return self.endpoint.id().to_bytes()

    def address(self, offer: lp.PairingOffer):
        return iroh.EndpointAddr(iroh.EndpointId.from_bytes(offer.computer_id), None, list(offer.addresses))

    async def pair(self, offer: lp.PairingOffer, proof: bytes | None = None) -> dict:
        connection = await self.endpoint.connect(self.address(offer), lp.PAIR_ALPN)
        stream = await connection.open_bi()
        proof = proof or lp.pairing_proof(offer.secret, offer.computer_id, self.id, self.name)
        await send_frame(stream.send(), lp.pair_request(self.name, proof, "1.1 (test)"))
        reply = await read_frame(stream.recv(), 10)
        connection.close(0, b"done")
        return reply

    async def link(self, offer: lp.PairingOffer) -> dict:
        self.connection = await self.endpoint.connect(self.address(offer), lp.LINK_ALPN)
        stream = await self.connection.open_bi()
        await send_frame(stream.send(), {"type": "hello", "v": 1, "device": self.name, "app": "1.1 (test)", "methods": ["status"], "time_zone": "Europe/Madrid"})
        reply = await read_frame(stream.recv(), 10)
        if reply.get("type") == "welcome":
            self.server = asyncio.create_task(self.serve())
        return reply

    async def serve(self):
        while True:
            try:
                stream = await self.connection.accept_bi()
            except iroh.IrohError:
                return
            task = asyncio.create_task(self.answer(stream))
            self.answering.add(task)
            task.add_done_callback(self.answering.discard)

    async def answer(self, stream):
        try:
            message = await read_frame(stream.recv(), 10)
        except (iroh.IrohError, asyncio.TimeoutError):
            return
        self.served.append(message)
        if self.frozen or message["method"] == "hang":
            return
        if message["method"] == "slow":
            await asyncio.sleep(1)
        if message["method"] == "fail":
            answer = {"v": 1, "id": message["id"], "ok": False, "as_of": AS_OF, "error": {"code": "invalid_params", "message": "Falta la fecha"}}
        else:
            answer = {"v": 1, "id": message["id"], "ok": True, "as_of": AS_OF, "result": {"method": message["method"], "params": message["params"]}}
        try:
            await send_frame(stream.send(), answer)
        except iroh.IrohError:
            pass

    async def revoke(self) -> dict:
        stream = await self.connection.open_bi()
        await send_frame(stream.send(), {"type": "revoke", "v": 1})
        return await read_frame(stream.recv(), 10)

    async def close(self):
        if self.server:
            self.server.cancel()
        for task in list(self.answering):
            task.cancel()
        if self.connection:
            self.connection.close(0, b"bye")
        await self.endpoint.close()


@unittest.skipIf(iroh is None, "needs the iroh package: uv run --with iroh==1.1.0 python -m unittest test_link_bridge")
class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.state = LinkState(home=Path(self.folder.name))
        self.daemon = LinkDaemon(self.state, name="Mac de prueba", relay=False, bind_addr="127.0.0.1:0")
        await self.daemon.start()
        self.phone = FakePhone()
        await self.phone.start()

    async def asyncTearDown(self):
        await self.phone.close()
        await self.daemon.shutdown()
        self.folder.cleanup()

    async def offer(self) -> lp.PairingOffer:
        created = await self.daemon.create_offer(120)
        offer = lp.PairingOffer.decode(created["text"])
        self.assertEqual(created["safety_code"], offer.safety_code())
        self.assertEqual(offer.name, "Mac de prueba")
        return offer

    async def test_pairing_stores_the_phone_once_and_confirms_with_the_secret(self):
        offer = await self.offer()
        reply = await self.phone.pair(offer)
        self.assertEqual(reply["type"], "paired")
        self.assertEqual(reply["computer_name"], "Mac de prueba")
        self.assertEqual(base64.b64decode(reply["confirmation"]), lp.pairing_confirmation(offer.secret, offer.computer_id, self.phone.id))
        self.assertEqual([p.id for p in self.state.phones()], [self.phone.id.hex()])
        self.assertEqual((await self.daemon.wait_for_pairing(1))["name"], "iPhone de prueba")
        again = await self.phone.pair(offer)
        self.assertEqual(again["type"], "paired", "the same iPhone retrying after a lost reply gets its confirmation again")
        self.assertEqual([p.id for p in self.state.phones()], [self.phone.id.hex()])

    async def test_a_second_device_with_the_same_code_leaves_neither_paired(self):
        offer = await self.offer()
        self.assertEqual((await self.phone.pair(offer))["type"], "paired")
        intruder = FakePhone("iPhone de otro")
        await intruder.start()
        try:
            crowded = await intruder.pair(offer)
        finally:
            await intruder.close()
        self.assertEqual(crowded, {"type": "pair_rejected", "v": 1, "reason": "crowded"})
        self.assertEqual(self.state.phones(), [])
        self.assertEqual((await self.phone.link(offer))["type"], "denied")
        with self.assertRaises(ControlError) as raised:
            await self.daemon.wait_for_pairing(1)
        self.assertEqual(raised.exception.code, "crowded")
        self.assertEqual((await self.phone.pair(offer))["reason"], "crowded")

    async def test_a_device_that_saw_the_code_cannot_take_over_an_iphone_already_linked(self):
        offer = await self.offer()
        await self.phone.pair(offer)
        await self.daemon.wait_for_pairing(1)
        await self.phone.link(offer)
        self.assertTrue(self.daemon.sessions)
        intruder = FakePhone("iPhone de Ana")
        await intruder.start()
        try:
            self.assertEqual((await intruder.pair(offer))["reason"], "crowded")
            self.assertEqual((await intruder.link(offer))["type"], "denied")
        finally:
            await intruder.close()
        self.assertEqual(self.state.phones(), [])
        self.assertEqual(self.daemon.sessions, {})
        with self.assertRaises(ControlError) as raised:
            await self.daemon.call("status", {}, 5)
        self.assertEqual(raised.exception.code, "not_connected")

    async def test_an_old_screenshot_of_the_code_cannot_unpair_the_iphone(self):
        offer = await self.offer()
        await self.phone.pair(offer)
        await self.daemon.wait_for_pairing(1)
        claimed = self.daemon.claimed
        claimed.offer = dataclasses.replace(claimed.offer, expires_at=int(claimed.offer.expires_at) - 3_600)
        intruder = FakePhone("iPhone de otro")
        await intruder.start()
        try:
            self.assertEqual((await intruder.pair(offer))["reason"], "no_offer")
        finally:
            await intruder.close()
        self.assertEqual([p.id for p in self.state.phones()], [self.phone.id.hex()])

    async def test_unpairing_on_the_computer_retires_the_code_that_paired_it(self):
        offer = await self.offer()
        await self.phone.pair(offer)
        self.daemon.unpair("iPhone de prueba")
        self.assertEqual((await self.phone.pair(offer))["reason"], "no_offer")
        self.assertEqual(self.state.phones(), [])

    async def test_a_wrong_proof_pairs_nothing(self):
        offer = await self.offer()
        reply = await self.phone.pair(offer, proof=bytes(32))
        self.assertEqual(reply["reason"], "invalid_proof")
        self.assertEqual(self.state.phones(), [])

    async def test_an_unpaired_phone_is_denied_the_link(self):
        offer = await self.offer()
        reply = await self.phone.link(offer)
        self.assertEqual(reply["type"], "denied")
        with self.assertRaises(ControlError) as raised:
            await self.daemon.call("status", {}, 5)
        self.assertEqual(raised.exception.code, "not_connected")

    async def test_calls_travel_to_the_phone_and_back_with_as_of(self):
        offer = await self.offer()
        await self.phone.pair(offer)
        welcome = await self.phone.link(offer)
        self.assertEqual(welcome["type"], "welcome")
        self.assertEqual(welcome["computer"], "Mac de prueba")
        reply = await self.daemon.call("search", {"query": "lunes"}, 5)
        self.assertEqual(reply["as_of"], AS_OF)
        self.assertEqual(reply["result"], {"method": "search", "params": {"query": "lunes"}})
        self.assertEqual(reply["path"], "direct")
        with self.assertRaises(ControlError) as raised:
            await self.daemon.call("fail", {}, 5)
        self.assertEqual(raised.exception.code, "remote:invalid_params")
        overview = self.daemon.status()
        self.assertTrue(overview["phones"][0]["connected"])
        results = await asyncio.gather(*(self.daemon.call("recent", {"hours": h}, 5) for h in range(1, 6)))
        self.assertEqual([r["result"]["params"]["hours"] for r in results], [1, 2, 3, 4, 5])

    async def test_the_link_closing_leaves_calls_saying_not_connected(self):
        offer = await self.offer()
        await self.phone.pair(offer)
        await self.phone.link(offer)
        self.phone.connection.close(0, b"background")
        for _ in range(50):
            if not self.daemon.sessions:
                break
            await asyncio.sleep(0.05)
        with self.assertRaises(ControlError) as raised:
            await self.daemon.call("status", {}, 5)
        self.assertEqual(raised.exception.code, "not_connected")
        self.assertIsNotNone(self.state.phones()[0].last_seen)

    async def test_a_phone_that_stops_answering_loses_the_link_instead_of_stalling_every_call(self):
        offer = await self.offer()
        await self.phone.pair(offer)
        await self.phone.link(offer)
        self.phone.frozen = True
        with self.assertRaises(ControlError) as raised:
            await self.daemon.call("status", {}, 0.5)
        self.assertEqual(raised.exception.code, "timeout")
        self.assertEqual(self.daemon.sessions, {})
        with self.assertRaises(ControlError) as raised:
            await self.daemon.call("status", {}, 5)
        self.assertEqual(raised.exception.code, "not_connected")

    async def test_a_slow_answer_from_a_phone_still_open_keeps_the_link(self):
        offer = await self.offer()
        await self.phone.pair(offer)
        await self.phone.link(offer)
        with self.assertRaises(ControlError) as raised:
            await self.daemon.call("hang", {}, 0.5)
        self.assertEqual(raised.exception.code, "slow")
        self.assertIn("ping", [m["method"] for m in self.phone.served])
        self.assertTrue(self.daemon.sessions)
        reply = await self.daemon.call("slow", {}, 5)
        self.assertEqual(reply["result"]["method"], "slow")

    async def test_revoking_from_the_phone_forgets_it_on_the_computer(self):
        offer = await self.offer()
        await self.phone.pair(offer)
        await self.phone.link(offer)
        self.assertEqual((await self.phone.revoke())["type"], "revoked")
        self.assertEqual(self.state.phones(), [])

    async def test_the_log_rotates_while_the_daemon_runs(self):
        original = link_daemon.LOG_BYTES
        link_daemon.LOG_BYTES = 300
        try:
            configure_logging(self.state, to_stderr=False)
            for index in range(20):
                logging.getLogger("evoca-link").info("line %d of a long day of answers", index)
        finally:
            link_daemon.LOG_BYTES = original
            logging.basicConfig(handlers=[logging.NullHandler()], force=True)
        self.assertTrue(self.state.log_path.with_name("daemon.log.1").exists())
        self.assertLess(self.state.log_path.stat().st_size, 400)

    async def test_control_requests_need_the_token(self):
        info = self.state.daemon_info()
        reader, writer = await asyncio.open_connection("127.0.0.1", info.port)
        writer.write(json.dumps({"token": "nope", "op": "status"}).encode() + b"\n")
        await writer.drain()
        reply = json.loads(await reader.readline())
        writer.close()
        self.assertEqual(reply["error"]["code"], "unauthorized")
        status = await request(self.state, {"op": "status"}, 5)
        self.assertEqual(status["computer"]["name"], "Mac de prueba")
        with self.assertRaises(ControlFailure):
            await request(self.state, {"op": "unpair", "phone": "nobody"}, 5)
        with self.assertRaises(ControlFailure) as raised:
            await request(self.state, {"op": "call", "method": "search", "params": {"query": "x" * 70_000}}, 5)
        self.assertEqual(raised.exception.code, "bad_request")


if __name__ == "__main__":
    unittest.main()
