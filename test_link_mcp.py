import asyncio
import contextlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

try:
    import iroh
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    iroh = None

from link_protocol import PairingOffer
from link_state import LinkState
from test_link_export import write_export

if iroh is not None:
    import link_mcp
    from link_client import ControlFailure, DaemonUnavailable
    from link_daemon import LinkDaemon, read_frame, send_frame
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp_smoke import tool_payload
    from test_link_bridge import AS_OF, FakePhone

HERE = Path(__file__).resolve().parent
TOOLS = {"status", "list_days", "read_day", "search", "moments", "recent", "transcript_since", "day_recap"}
UNPAIRED = "No iPhone is paired with this computer yet"


def hermetic_environment(link_home: str, home: str | None = None, export: Path | None = None) -> dict[str, str]:
    """The computer where the bridge runs may hold a real export in iCloud Drive: the server sees an empty home unless the test builds one."""
    environment = {key: value for key, value in os.environ.items() if key != "EVOCA_EXPORT_DIR"}
    environment.update(EVOCA_LINK_HOME=link_home, HOME=home or link_home, USERPROFILE=home or link_home)
    if export is not None:
        environment["EVOCA_EXPORT_DIR"] = str(export)
    return environment


@unittest.skipIf(iroh is None, "needs iroh and mcp: uv run --with iroh==1.1.0 --with 'mcp>=2.3,<3' --with segno python -m unittest test_link_mcp")
class McpOverTheLinkTests(unittest.IsolatedAsyncioTestCase):
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

    def parameters(self) -> "StdioServerParameters":
        return StdioServerParameters(command=sys.executable, args=[str(HERE / "evoca_link.py"), "mcp"], env=hermetic_environment(self.folder.name))

    async def call(self, session, name: str, arguments: dict) -> tuple[bool, dict | str]:
        result = await session.call_tool(name, arguments)
        text = "".join(getattr(block, "text", "") for block in result.content)
        return (False, text) if result.is_error else (True, tool_payload(result.structured_content, text))

    async def test_tools_reach_the_phone_and_say_when_it_is_away(self):
        from link_protocol import PairingOffer

        offer = PairingOffer.decode((await self.daemon.create_offer(120))["text"])
        await self.phone.pair(offer)
        async with stdio_client(self.parameters()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self.assertEqual({tool.name for tool in (await session.list_tools()).tools}, TOOLS)

                ok, status = await self.call(session, "status", {})
                self.assertTrue(ok)
                self.assertFalse(status["connected"])
                self.assertIn("open Evoca", status["hint"])
                ok, message = await self.call(session, "recent", {"hours": 2})
                self.assertFalse(ok)
                self.assertIn("not connected", message)

                await self.phone.link(offer)
                ok, hits = await self.call(session, "search", {"query": "lunes", "limit": 500})
                self.assertTrue(ok, hits)
                self.assertEqual(hits["as_of"], AS_OF)
                self.assertEqual(hits["source"], "iphone (live)")
                self.assertEqual(hits["phone"], "iPhone de prueba")
                self.assertEqual(hits["params"], {"query": "lunes", "since": None, "until": None, "limit": 100, "include_on_screen": False})
                ok, transcript = await self.call(session, "transcript_since", {"minutes": 999, "wait_seconds": 90})
                self.assertEqual(transcript["params"], {"minutes": 120, "wait_seconds": 45, "include_on_screen": False})
                ok, status = await self.call(session, "status", {})
                self.assertTrue(status["connected"])
                self.assertEqual(status["iphone"]["method"], "status")
                ok, failure = await self.call(session, "search", {"query": "  "})
                self.assertFalse(ok)

                self.phone.result_extra = {"engine": "Demostración · v4"}
                ok, message = await self.call(session, "day_recap", {"day": "hoy"})
                self.assertFalse(ok)
                self.assertIn("demo data", message)
                self.phone.result_extra = {}
                ok, status = await self.call(session, "status", {})
                self.assertFalse(status["connected"])
                self.assertTrue(status["phones"][0]["demo"])
                self.assertIn("demo data", status["hint"])

                self.phone.connection.close(0, b"background")
                for _ in range(60):
                    if not self.daemon.sessions:
                        break
                    await asyncio.sleep(0.05)
                ok, message = await self.call(session, "day_recap", {"day": "ayer"})
                self.assertFalse(ok)
                self.assertIn("open Evoca on the iPhone", message)


if iroh is not None:
    class ScriptedPhone(FakePhone):
        def __init__(self):
            super().__init__()
            self.refused: set[str] = set()
            self.ignored: set[str] = set()

        async def answer(self, stream):
            try:
                message = await read_frame(stream.recv(), 10)
            except (iroh.IrohError, asyncio.TimeoutError):
                return
            self.served.append(message)
            if self.frozen or message["method"] in self.ignored:
                return
            if message["method"] in self.refused:
                reply = {"v": 1, "id": message["id"], "ok": False, "as_of": AS_OF, "error": {"code": "invalid_params", "message": "Falta la fecha"}}
            else:
                reply = {"v": 1, "id": message["id"], "ok": True, "as_of": AS_OF, "result": {"method": message["method"], "params": message["params"]}}
            try:
                await send_frame(stream.send(), reply)
            except iroh.IrohError:
                pass


@unittest.skipIf(iroh is None, "needs iroh and mcp: uv run --with iroh==1.1.0 --with 'mcp>=2.3,<3' --with segno python -m unittest test_link_mcp")
class ExportFallbackCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.state = LinkState(home=Path(self.folder.name))
        self.daemon = LinkDaemon(self.state, name="Mac de prueba", relay=False, bind_addr="127.0.0.1:0")
        await self.daemon.start()
        self.phone = ScriptedPhone()
        await self.phone.start()
        self.offer = PairingOffer.decode((await self.daemon.create_offer(120))["text"])
        self.exports = tempfile.TemporaryDirectory()
        self.addCleanup(self.exports.cleanup)
        now = datetime.now().astimezone().replace(microsecond=0)
        self.base = now - timedelta(minutes=20)
        if (self.base + timedelta(minutes=6)).date() != self.base.date():
            self.base -= timedelta(minutes=30)
        self.export = write_export(Path(self.exports.name), self.base, exported_at=now - timedelta(minutes=1))
        self.written = json.loads((self.export / "manifest.json").read_text(encoding="utf-8"))["days"]
        self.newest = max(self.written.values(), key=datetime.fromisoformat)
        self.day = self.base.date().isoformat()

    async def asyncTearDown(self):
        await self.phone.close()
        await self.daemon.shutdown()

    def calls(self) -> list[tuple[str, dict]]:
        return [
            ("list_days", {"limit": 5}),
            ("read_day", {"day": self.day}),
            ("search", {"query": "presupuesto"}),
            ("moments", {}),
            ("recent", {"hours": 1}),
            ("transcript_since", {"minutes": 90, "wait_seconds": 0}),
            ("day_recap", {"day": self.day}),
        ]

    @contextlib.asynccontextmanager
    async def session(self, export: Path | None = None, home: Path | None = None):
        parameters = StdioServerParameters(command=sys.executable, args=[str(HERE / "evoca_link.py"), "mcp"], env=hermetic_environment(self.folder.name, str(home) if home else None, export))
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def call(self, session, name: str, arguments: dict) -> tuple[bool, dict | str]:
        result = await session.call_tool(name, arguments)
        text = "".join(getattr(block, "text", "") for block in result.content)
        return (False, text) if result.is_error else (True, tool_payload(result.structured_content, text))

    async def link(self) -> None:
        await self.phone.pair(self.offer)
        await self.phone.link(self.offer)
        for _ in range(100):
            if self.daemon.sessions:
                return
            await asyncio.sleep(0.05)
        self.fail("the daemon never registered the link")

    async def drop_link(self) -> None:
        self.phone.connection.close(0, b"background")
        for _ in range(100):
            if not self.daemon.sessions:
                return
            await asyncio.sleep(0.05)
        self.fail("the daemon kept the link")

    def assert_from_export(self, tool: str, result: dict) -> None:
        self.assertEqual(result["source"], "export (stale)", tool)
        self.assertIs(result["stale"], True, tool)
        self.assertTrue(result["iphone_unreachable"].startswith("The iPhone (iPhone de prueba) is not connected right now."), tool)
        self.assertIn("not live data", result["note"], tool)
        self.assertNotIn("phone", result, tool)
        self.assertNotIn("path", result, tool)
        self.assertEqual(result["as_of"], self.written[self.day] if tool in ("read_day", "day_recap") else self.newest, tool)
        self.assertNotIn("warnings", result, tool)

    def assert_content(self, tool: str, result: dict) -> None:
        if tool == "list_days":
            self.assertIn(self.day, [row["date"] for row in result["days"]])
        elif tool == "read_day":
            self.assertIn("Vamos a tomar un café con el proveedor", result["markdown"])
        elif tool == "search":
            self.assertEqual([hit["match"] for hit in result["hits"]], ["words"])
        elif tool == "moments":
            self.assertIn("Presupuesto", [row["title"] for row in result["moments"]])
        elif tool == "recent":
            self.assertIn("Mañana revisamos la entrega de los tornillos", [p["text"] for p in result["phrases"]])
        elif tool == "transcript_since":
            self.assertIn("Mañana revisamos la entrega de los tornillos", [p["text"] for p in result["phrases"]])
            self.assertIs(result["complete"], False)
            self.assertTrue(result["coverage"]["unknown"])
        elif tool == "day_recap":
            self.assertEqual(result["recap"]["headline"], "Presupuesto del almacén cerrado")

    def assert_live(self, tool: str, result: dict) -> None:
        self.assertEqual(result["source"], "iphone (live)", tool)
        self.assertEqual(result["phone"], "iPhone de prueba", tool)
        self.assertEqual(result["as_of"], AS_OF, tool)
        self.assertEqual(result["method"], tool)
        self.assertNotIn("stale", result, tool)
        self.assertNotIn("iphone_unreachable", result, tool)


class ExportFallbackOverMcpTests(ExportFallbackCase):
    async def test_every_reading_tool_answers_from_the_export_when_the_paired_phone_is_not_connected(self):
        await self.phone.pair(self.offer)
        async with self.session(self.export) as session:
            for tool, arguments in self.calls():
                with self.subTest(tool=tool):
                    ok, result = await self.call(session, tool, arguments)
                    self.assertTrue(ok, result)
                    self.assert_from_export(tool, result)
                    self.assert_content(tool, result)
            ok, status = await self.call(session, "status", {})
            self.assertTrue(ok, status)
            self.assertFalse(status["connected"])
            self.assertEqual(status["source"], "computer")
            self.assertEqual(status["export"], {"found": True, "path": str(self.export), "last_written": self.newest})
        self.assertEqual(self.phone.served, [])

    async def test_a_connected_phone_answers_live_and_never_from_the_export(self):
        await self.link()
        async with self.session(self.export) as session:
            for tool, arguments in self.calls():
                with self.subTest(tool=tool):
                    ok, result = await self.call(session, tool, arguments)
                    self.assertTrue(ok, result)
                    self.assert_live(tool, result)
            ok, status = await self.call(session, "status", {})
            self.assertTrue(status["connected"])
            self.assertEqual(status["source"], "iphone (live)")
            self.assertTrue(status["export"]["found"])
        self.assertEqual([m["method"] for m in self.phone.served if m["method"] != "ping"], [tool for tool, _ in self.calls()] + ["status"])

    async def test_when_the_link_drops_the_tools_fall_back_to_the_export_and_come_back_live_with_it(self):
        await self.link()
        async with self.session(self.export) as session:
            ok, live = await self.call(session, "recent", {"hours": 1})
            self.assert_live("recent", live)
            await self.drop_link()
            for tool, arguments in self.calls():
                with self.subTest(tool=tool):
                    ok, result = await self.call(session, tool, arguments)
                    self.assertTrue(ok, result)
                    self.assert_from_export(tool, result)
            await self.phone.link(self.offer)
            for _ in range(100):
                if self.daemon.sessions:
                    break
                await asyncio.sleep(0.05)
            ok, again = await self.call(session, "recent", {"hours": 1})
            self.assert_live("recent", again)

    async def test_without_a_paired_phone_nothing_is_read_from_the_export(self):
        async with self.session(self.export) as session:
            for tool, arguments in self.calls():
                with self.subTest(tool=tool):
                    ok, message = await self.call(session, tool, arguments)
                    self.assertFalse(ok)
                    self.assertIn(UNPAIRED, message)
                    self.assertNotIn("export", message.lower())

    async def test_unpairing_stops_the_export_from_being_served(self):
        await self.link()
        async with self.session(self.export) as session:
            ok, live = await self.call(session, "search", {"query": "presupuesto"})
            self.assert_live("search", live)
            self.daemon.unpair("iPhone de prueba")
            self.assertEqual(self.state.phones(), [])
            for tool, arguments in self.calls():
                with self.subTest(tool=tool):
                    ok, message = await self.call(session, tool, arguments)
                    self.assertFalse(ok)
                    self.assertIn(UNPAIRED, message)
                    self.assertNotIn("export", message.lower())

    async def test_revoking_from_the_phone_stops_the_export_from_being_served_too(self):
        await self.link()
        async with self.session(self.export) as session:
            self.assertEqual((await self.phone.revoke())["type"], "revoked")
            for _ in range(100):
                if not self.state.phones() and not self.daemon.sessions:
                    break
                await asyncio.sleep(0.05)
            ok, message = await self.call(session, "recent", {"hours": 1})
            self.assertFalse(ok)
            self.assertIn(UNPAIRED, message)

    async def test_a_paired_phone_and_no_export_keeps_the_old_error_and_says_how_to_point_to_one(self):
        await self.phone.pair(self.offer)
        old = link_mcp.Bridge(self.state, HERE / "evoca_link.py").not_connected()
        async with self.session() as session:
            for tool, arguments in self.calls():
                with self.subTest(tool=tool):
                    ok, message = await self.call(session, tool, arguments)
                    self.assertFalse(ok)
                    self.assertIn(old + " No export of Evoca was found to answer from instead (none in iCloud Drive or ~/Library/CloudStorage): turn on «Export Automatically»", message)
                    self.assertIn("set EVOCA_EXPORT_DIR to the exported Evoca folder (the one that holds manifest.json).", message)
            ok, status = await self.call(session, "status", {})
            self.assertFalse(status["export"]["found"])
            self.assertIn("EVOCA_EXPORT_DIR", status["export"]["hint"])
        empty = Path(self.exports.name) / "empty"
        empty.mkdir()
        async with self.session(export=empty) as session:
            ok, message = await self.call(session, "search", {"query": "presupuesto"})
            self.assertFalse(ok)
            self.assertIn(old + " No export of Evoca was found to answer from instead (no manifest.json in", message)

    async def test_the_export_in_icloud_drive_is_found_without_any_setting(self):
        home = Path(self.exports.name) / "home"
        write_export(home / "Library" / "Mobile Documents" / "com~apple~CloudDocs", self.base, exported_at=datetime.fromisoformat(self.newest))
        await self.phone.pair(self.offer)
        async with self.session(home=home) as session:
            ok, result = await self.call(session, "recent", {"hours": 1})
            self.assertTrue(ok, result)
            self.assert_from_export("recent", result)
            ok, status = await self.call(session, "status", {})
            self.assertTrue(status["export"]["path"].endswith("com~apple~CloudDocs/Evoca"))

    async def test_a_refusal_from_the_iphone_is_never_replaced_by_the_export(self):
        await self.link()
        self.phone.refused.add("recent")
        async with self.session(self.export) as session:
            ok, message = await self.call(session, "recent", {"hours": 1})
            self.assertFalse(ok)
            self.assertIn("The iPhone answered: Falta la fecha", message)
            self.assertNotIn("export", message.lower())
            ok, other = await self.call(session, "search", {"query": "presupuesto"})
            self.assert_live("search", other)

    async def test_an_invalid_day_is_an_error_even_when_answering_from_the_export(self):
        await self.phone.pair(self.offer)
        async with self.session(self.export) as session:
            ok, message = await self.call(session, "read_day", {"day": "el martes"})
            self.assertFalse(ok)
            self.assertIn("Invalid date «el martes»: use hoy, ayer or YYYY-MM-DD.", message)


class ExportFallbackInProcessTests(ExportFallbackCase):
    """The tools wait 30 s before they give up on the phone, so the failures that need a deadline run through the bridge with a short one."""

    def bridge(self) -> "link_mcp.Bridge":
        return link_mcp.Bridge(self.state, HERE / "evoca_link.py")

    async def test_a_busy_phone_that_is_still_connected_never_falls_back(self):
        await self.link()
        self.phone.ignored.add("recent")
        with mock.patch.dict(os.environ, {"EVOCA_EXPORT_DIR": str(self.export)}):
            with self.assertRaises(ToolError) as raised:
                await self.bridge().call("recent", {"hours": 1, "include_on_screen": False}, timeout=0.5)
        self.assertIn("is connected but did not finish", str(raised.exception))
        self.assertTrue(self.daemon.sessions)

    async def test_a_phone_that_stops_answering_falls_back_to_the_export(self):
        await self.link()
        self.phone.frozen = True
        bridge = self.bridge()
        with mock.patch.dict(os.environ, {"EVOCA_EXPORT_DIR": str(self.export)}):
            result = await bridge.call("recent", {"hours": 1, "include_on_screen": False}, timeout=0.5)
            self.assertEqual(self.daemon.sessions, {})
            self.assertEqual(result["source"], "export (stale)")
            self.assertTrue(result["iphone_unreachable"].startswith("The iPhone stopped answering, so the bridge dropped that link."))
            self.assertEqual(result["as_of"], self.newest)
            self.assertIn("Mañana revisamos la entrega de los tornillos", [p["text"] for p in result["phrases"]])
            again = await bridge.call("recent", {"hours": 1, "include_on_screen": False}, timeout=0.5)
            self.assertTrue(again["iphone_unreachable"].startswith("The iPhone (iPhone de prueba) is not connected right now."))

    async def test_status_is_never_answered_from_the_export(self):
        await self.phone.pair(self.offer)
        with mock.patch.dict(os.environ, {"EVOCA_EXPORT_DIR": str(self.export)}):
            with self.assertRaises(ToolError) as raised:
                await self.bridge().call("status", {}, timeout=0.5)
        self.assertIn("is not connected right now", str(raised.exception))
        self.assertNotIn("export", str(raised.exception).lower())


@unittest.skipIf(iroh is None, "needs iroh and mcp: uv run --with iroh==1.1.0 --with 'mcp>=2.3,<3' --with segno python -m unittest test_link_mcp")
class ExportFallbackDecisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.state = LinkState(home=Path(self.folder.name) / "link")
        self.state.add_phone("ab" * 32, "iPhone de prueba", None)
        self.export = write_export(Path(self.folder.name) / "exports", datetime.now().astimezone().replace(microsecond=0) - timedelta(minutes=20))
        self.bridge = link_mcp.Bridge(self.state, HERE / "evoca_link.py")
        environment = mock.patch.dict(os.environ, {"EVOCA_EXPORT_DIR": str(self.export)})
        environment.start()
        self.addCleanup(environment.stop)

    async def call_failing(self, failure: Exception, method: str = "recent", params: dict | None = None) -> dict:
        with mock.patch.object(link_mcp, "ensure_daemon", new=mock.AsyncMock()), mock.patch.object(link_mcp, "request", new=mock.AsyncMock(side_effect=failure)):
            return await self.bridge.call(method, params or {"hours": 1, "include_on_screen": False})

    async def test_only_an_iphone_that_does_not_answer_is_replaced_by_the_export(self):
        not_connected = await self.call_failing(ControlFailure("not_connected", "not connected"))
        self.assertEqual(not_connected["source"], "export (stale)")
        self.assertEqual(not_connected["iphone_unreachable"], self.bridge.not_connected().strip())
        stopped = await self.call_failing(ControlFailure("timeout", "The iPhone stopped answering"))
        self.assertEqual(stopped["source"], "export (stale)")
        self.assertEqual(stopped["iphone_unreachable"], "The iPhone stopped answering, so the bridge dropped that link. If Evoca is still open it reconnects by itself within seconds: try again; otherwise ask the user to open Evoca.")

    async def test_every_other_failure_stays_an_error(self):
        for code in ("slow", "remote:invalid_params", "remote:unavailable", "remote:internal", "internal", "bad_request", "unauthorized", "demo", "not_found"):
            with self.subTest(code=code):
                with self.assertRaises(ToolError) as raised:
                    await self.call_failing(ControlFailure(code, "refused"))
                self.assertNotIn("export", str(raised.exception).lower())

    async def test_a_bridge_that_is_not_running_stays_an_error(self):
        with mock.patch.object(link_mcp, "ensure_daemon", new=mock.AsyncMock(side_effect=DaemonUnavailable("the evoca-link daemon did not start"))):
            with self.assertRaises(ToolError) as raised:
                await self.bridge.call("recent", {"hours": 1, "include_on_screen": False})
        self.assertIn("The evoca-link bridge on this computer is not running", str(raised.exception))
        self.assertNotIn("export", str(raised.exception).lower())

    async def test_tools_the_export_cannot_answer_stay_errors(self):
        for method in ("status", "ping", "unknown_method"):
            with self.subTest(method=method):
                with self.assertRaises(ToolError) as raised:
                    await self.call_failing(ControlFailure("not_connected", "not connected"), method=method, params={})
                self.assertNotIn("export", str(raised.exception).lower())

    async def test_no_paired_iphone_means_no_export_whatever_the_failure(self):
        self.state.remove_phone("ab" * 32)
        for code in ("not_connected", "timeout"):
            with self.subTest(code=code):
                with self.assertRaises(ToolError) as raised:
                    await self.call_failing(ControlFailure(code, "gone"))
                self.assertNotIn("export", str(raised.exception).lower())
        with self.assertRaises(ToolError) as raised:
            await self.call_failing(ControlFailure("not_connected", "gone"))
        self.assertIn(UNPAIRED, str(raised.exception))

    async def test_the_export_is_looked_for_again_when_it_moves(self):
        first = await self.call_failing(ControlFailure("not_connected", "gone"))
        self.assertEqual(first["source"], "export (stale)")
        moved = self.export.with_name("moved")
        self.export.rename(moved)
        with mock.patch.dict(os.environ, {"EVOCA_EXPORT_DIR": str(moved)}):
            second = await self.call_failing(ControlFailure("not_connected", "gone"))
        self.assertEqual(second["source"], "export (stale)")
        self.assertEqual(self.bridge.export_root, moved)

    async def test_an_invalid_day_is_the_callers_error(self):
        with self.assertRaises(ToolError) as raised:
            await self.call_failing(ControlFailure("not_connected", "gone"), method="day_recap", params={"day": "pasado mañana"})
        self.assertIn("Invalid date «pasado mañana»", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
