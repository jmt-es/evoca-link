#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["iroh==1.1.0", "mcp>=2.3,<3", "segno>=1.6,<2"]
# ///
import argparse
import asyncio
import json
import locale
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from link_client import ControlFailure, DaemonUnavailable, ensure_daemon, request
from link_protocol import display_code
from link_state import LinkState

SCRIPT = Path(__file__).resolve()
PAIR_POLL_SECONDS = 30


def spanish() -> bool:
    language = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or (locale.getlocale()[0] or "")
    return language.lower().startswith("es")


def t(english: str, spanish_text: str) -> str:
    return spanish_text if spanish() else english


def mcp_command() -> str:
    return f'uv run --quiet --script "{SCRIPT}" mcp'


def print_qr(text: str) -> None:
    import segno

    code = segno.make(text, error="l")
    code.terminal(out=sys.stdout, compact=True, border=2)


async def pair(state: LinkState, ttl: int) -> int:
    await ensure_daemon(state, SCRIPT)
    offer = await request(state, {"op": "pair", "ttl": ttl}, 20)
    minutes = max(1, round((offer["expires_at"] - time.time()) / 60))
    print(t(f"Pair «{offer['computer']}» with Evoca", f"Emparejar «{offer['computer']}» con Evoca"))
    print(t("On the iPhone: Evoca › Settings › Your Agents › Connect to a Computer (Ajustes › Tus agentes › Conectar con un ordenador in Spanish)", "En el iPhone: Evoca › Ajustes › Tus agentes › Conectar con un ordenador (Settings › Your Agents › Connect to a Computer en inglés)"))
    print()
    print_qr(offer["text"])
    print(t(f"Safety code: {display_code(offer['safety_code'])}  (the iPhone shows the same six digits)", f"Código de seguridad: {display_code(offer['safety_code'])}  (el iPhone muestra los mismos seis dígitos)"))
    print(t(f"No camera? Copy this code and use «Paste Code» («Pegar código» in Spanish) on the iPhone. It works once and expires in {minutes} min:", f"¿Sin cámara? Copia este código y usa «Pegar código» («Paste Code» en inglés) en el iPhone. Sirve una vez y caduca en {minutes} min:"))
    print(offer["text"])
    print()
    print(t("Waiting for the iPhone…", "Esperando al iPhone…"), flush=True)
    try:
        while True:
            try:
                reply = await request(state, {"op": "pair_wait", "timeout": PAIR_POLL_SECONDS}, PAIR_POLL_SECONDS + 10)
                break
            except ControlFailure as failure:
                if failure.code == "timeout":
                    continue
                if failure.code == "expired":
                    print(t("The code expired. Run `evoca-link pair` again.", "El código ha caducado. Vuelve a ejecutar `evoca-link pair`."))
                    return 1
                if failure.code == "crowded":
                    print(t("Two devices used this code, so someone else saw it: neither is paired. Run `evoca-link pair` again and keep the QR off shared screens.",
                            "Dos dispositivos usaron este código, así que alguien más lo vio: no se ha emparejado ninguno. Vuelve a ejecutar `evoca-link pair` y no enseñes el QR en pantallas compartidas."))
                    return 1
                raise
    except (KeyboardInterrupt, asyncio.CancelledError):
        await request(state, {"op": "pair_cancel"}, 5)
        print(t("Cancelled.", "Cancelado."))
        return 130
    phone = reply["phone"]
    print(t(f"Paired with {phone.get('name', 'the iPhone')}.", f"Emparejado con {phone.get('name', 'el iPhone')}."))
    print()
    print(t("Give it to your agents (once per computer):", "Conéctalo a tus agentes (una vez por ordenador):"))
    print(f"  claude mcp add --scope user evoca-link -- {mcp_command()}")
    print(f"  codex mcp add evoca-link -- {mcp_command()}")
    print(t("The link is live while Evoca is open on the iPhone.", "El enlace está vivo mientras Evoca esté abierta en el iPhone."))
    return 0


def when(timestamp: float | None) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M") if timestamp else "—"


async def status(state: LinkState, as_json: bool) -> int:
    try:
        overview = await request(state, {"op": "status"}, 10)
    except DaemonUnavailable:
        overview = None
    if as_json:
        print(json.dumps(overview or {"running": False, "phones": [p.__dict__ for p in state.phones()]}, ensure_ascii=False, indent=2))
        return 0
    if overview is None:
        print(t("Bridge: stopped (it starts by itself with `evoca-link mcp` or `evoca-link pair`)", "Puente: parado (arranca solo con `evoca-link mcp` o `evoca-link pair`)"))
        phones = [p.__dict__ | {"connected": False} for p in state.phones()]
    else:
        computer = overview["computer"]
        print(t(f"Bridge: running as «{computer['name']}» ({computer['id'][:10]}…)", f"Puente: en marcha como «{computer['name']}» ({computer['id'][:10]}…)"))
        print(t(f"Relay: {computer['relay_url'] or 'none (direct only)'}", f"Relé: {computer['relay_url'] or 'ninguno (solo directo)'}"))
        phones = overview["phones"]
    if not phones:
        print(t("No iPhone paired. Run `evoca-link pair`.", "Ningún iPhone emparejado. Ejecuta `evoca-link pair`."))
    for phone in phones:
        if phone.get("connected"):
            path = phone.get("path") or {}
            detail = f"{path.get('kind', '?')}, {path.get('rtt_ms', '?')} ms"
            print(t(f"• {phone['name']}: connected since {when(phone['connected_since'])} ({detail})", f"• {phone['name']}: conectado desde {when(phone['connected_since'])} ({detail})"))
        else:
            print(t(f"• {phone['name']}: not connected, last seen {when(phone.get('last_seen'))}", f"• {phone['name']}: sin conexión, visto por última vez {when(phone.get('last_seen'))}"))
    print(t(f"MCP: claude mcp add --scope user evoca-link -- {mcp_command()}", f"MCP: claude mcp add --scope user evoca-link -- {mcp_command()}"))
    return 0


async def unpair(state: LinkState, phone: str) -> int:
    try:
        reply = await request(state, {"op": "unpair", "phone": phone}, 10)
        removed = reply["removed"]
    except DaemonUnavailable:
        match = state.find_phone(phone)
        if match is None:
            print(t(f"No paired iPhone matches «{phone}».", f"Ningún iPhone emparejado coincide con «{phone}»."))
            return 1
        state.remove_phone(match.id)
        removed = {"name": match.name}
    print(t(f"Forgot {removed['name']}. To pair again, run `evoca-link pair`.", f"Olvidado {removed['name']}. Para volver a emparejar, ejecuta `evoca-link pair`."))
    return 0


async def call(state: LinkState, method: str, params: str | None) -> int:
    await ensure_daemon(state, SCRIPT)
    reply = await request(state, {"op": "call", "method": method, "params": json.loads(params) if params else {}, "timeout": 60}, 75)
    print(json.dumps({"as_of": reply["as_of"], "phone": reply["phone"], "path": reply["path"], "result": reply["result"]}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="evoca-link", description="Live end-to-end encrypted link between Evoca on the iPhone and this computer, exposed to agents over MCP.")
    commands = parser.add_subparsers(dest="command", required=True)
    pair_command = commands.add_parser("pair", help="show a QR to pair an iPhone")
    pair_command.add_argument("--ttl", type=int, default=300, help="seconds the code stays valid (default 300)")
    status_command = commands.add_parser("status", help="bridge, paired iPhones and the live link")
    status_command.add_argument("--json", action="store_true")
    unpair_command = commands.add_parser("unpair", help="forget a paired iPhone")
    unpair_command.add_argument("phone", help="name or the start of its id")
    daemon_command = commands.add_parser("daemon", help="run the bridge that keeps the link (started automatically)")
    daemon_command.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    daemon_command.add_argument("--no-relay", action="store_true", help="direct connections only (same network or VPN)")
    daemon_command.add_argument("--bind", help="UDP address to bind, host:port")
    daemon_command.add_argument("--name", help="computer name shown on the iPhone")
    commands.add_parser("stop", help="stop the bridge")
    commands.add_parser("mcp", help="MCP server over stdio for Claude Code, Codex and others")
    call_command = commands.add_parser("call", help="call one iPhone method and print the JSON (debugging)")
    call_command.add_argument("method")
    call_command.add_argument("params", nargs="?", help="JSON object")
    args = parser.parse_args()
    state = LinkState()

    if args.command == "mcp":
        from link_mcp import run
        run(state, SCRIPT)
        return 0
    if args.command == "daemon":
        from link_daemon import configure_logging, run_daemon
        configure_logging(state, to_stderr=not args.background)
        return asyncio.run(run_daemon(state, name=args.name, relay=not args.no_relay, bind_addr=args.bind))
    try:
        if args.command == "pair":
            return asyncio.run(pair(state, args.ttl))
        if args.command == "status":
            return asyncio.run(status(state, args.json))
        if args.command == "unpair":
            return asyncio.run(unpair(state, args.phone))
        if args.command == "stop":
            try:
                asyncio.run(request(state, {"op": "stop"}, 10))
            except DaemonUnavailable:
                pass
            print(t("Bridge stopped.", "Puente parado."))
            return 0
        if args.command == "call":
            return asyncio.run(call(state, args.method, args.params))
    except DaemonUnavailable as error:
        print(f"evoca-link: {error}", file=sys.stderr)
        return 2
    except ControlFailure as failure:
        print(f"evoca-link: {failure.message} ({failure.code})", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
