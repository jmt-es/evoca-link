import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import iroh
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    iroh = None

from link_state import LinkState

if iroh is not None:
    from link_daemon import LinkDaemon
    from mcp_smoke import tool_payload
    from test_link_bridge import AS_OF, FakePhone

HERE = Path(__file__).resolve().parent
TOOLS = {"status", "list_days", "read_day", "search", "moments", "recent", "transcript_since", "day_recap"}


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
        return StdioServerParameters(command=sys.executable, args=[str(HERE / "evoca_link.py"), "mcp"], env=dict(os.environ, EVOCA_LINK_HOME=self.folder.name))

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


if __name__ == "__main__":
    unittest.main()
