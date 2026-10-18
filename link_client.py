import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from link_state import LinkState

PING_SECONDS = 3
START_SECONDS = 15
REPLY_LIMIT = 64 * 1024 * 1024


class DaemonUnavailable(Exception):
    pass


class ControlFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def request(state: LinkState, payload: dict, timeout: float) -> dict:
    info = state.daemon_info()
    if info is None:
        raise DaemonUnavailable("the evoca-link daemon is not running")
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", info.port, limit=REPLY_LIMIT), PING_SECONDS)
    except (OSError, asyncio.TimeoutError) as error:
        raise DaemonUnavailable("the evoca-link daemon is not running") from error
    try:
        writer.write(json.dumps({"token": info.token, **payload}, ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
    except (OSError, asyncio.TimeoutError) as error:
        raise DaemonUnavailable("the evoca-link daemon did not answer") from error
    finally:
        writer.close()
    if not line:
        raise DaemonUnavailable("the evoca-link daemon closed the connection")
    try:
        reply = json.loads(line.decode("utf-8"))
    except ValueError as error:
        raise DaemonUnavailable("something else answered on the evoca-link port") from error
    if not reply.get("ok"):
        error = reply.get("error") or {}
        raise ControlFailure(str(error.get("code", "internal")), str(error.get("message", "")))
    return reply


def spawn_daemon(state: LinkState, script: Path) -> None:
    """The daemon's own output goes to daemon.out, never daemon.log: Windows cannot rotate a log that the daemon also holds open as its stdout."""
    state.ensure()
    with open(state.out_path, "wb") as output:
        options: dict = {"stdin": subprocess.DEVNULL, "stdout": output, "stderr": output, "close_fds": True}
        if os.name == "nt":
            options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            options["start_new_session"] = True
        environment = dict(os.environ, EVOCA_LINK_HOME=str(state.home))
        subprocess.Popen([sys.executable, str(script), "daemon", "--background"], env=environment, **options)


async def ensure_daemon(state: LinkState, script: Path) -> None:
    try:
        await request(state, {"op": "ping"}, PING_SECONDS)
        return
    except (DaemonUnavailable, ControlFailure):
        pass
    spawn_daemon(state, script)
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        try:
            await request(state, {"op": "ping"}, PING_SECONDS)
            return
        except (DaemonUnavailable, ControlFailure):
            continue
    raise DaemonUnavailable(f"the evoca-link daemon did not start; see {state.log_path} and {state.out_path}")
