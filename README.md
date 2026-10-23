# evoca-link

English · [Español](#español)

The computer side of **Evoca Link**: a live, end-to-end encrypted link between [Evoca](https://evoca.day) on your iPhone and your computer. While Evoca is open on the iPhone, Claude Code, Codex or any other MCP client on your computer can ask the iPhone directly: what it has stored, which days exist, a whole day, a search (by words and by meaning), marked moments, what was said recently, "what was just said in the last N minutes" and the recap of a day. Read-only.

```text
Claude Code / Codex ──stdio MCP──▶ evoca-link mcp ──localhost──▶ evoca-link daemon ◀══ iroh (QUIC + TLS 1.3) ══ Evoca on the iPhone
```

Evoca is an all-day recorder for Apple Watch that transcribes on the iPhone. This repository is everything that runs on the computer: a [uv](https://docs.astral.sh/uv/) script with a pairing command, a small background daemon that keeps the link, and an MCP server. The iPhone side is built into the Evoca app.

- **The iPhone calls the computer.** iOS does not let an app accept connections while it is suspended, so Evoca dials the computer when it opens and hangs up when it goes to the background. The computer waits with the daemon (the bridge), which keeps the link and shares it between all your agents.
- **The iPhone answers.** Every answer is computed on the iPhone from its own archive (the same hybrid search as Evoca's Search, the same window as Rewind) and carries `as_of`, the iPhone's clock when it answered.

## What it sends, and what it does not

- **Point to point, end-to-end encrypted.** The two ends are the iPhone and your computer, paired by their Ed25519 keys. The transport is [iroh](https://iroh.computer) 1.x: QUIC with TLS 1.3 between those two keys. It goes directly when the network allows it (same Wi-Fi, or through NAT) and, when it cannot, through a public relay run by n0, the company behind iroh, which forwards data it cannot read.
- **Nothing goes through Evoca servers**, because Evoca has no servers and no accounts. Apart from the link itself (and the relay and discovery services that iroh uses), the program contacts nothing: no telemetry, no analytics, no update checks (uv downloads the dependencies the first time).
- **What an agent can read**, while Evoca is open and only after you paired the computer: transcripts, marked moments, named places and day recaps, plus the state of the archive (days, minutes recorded, fragments waiting for transcription, whether the watch or the iPhone is recording). It is read-only: no tool changes anything on the iPhone.
- **What never leaves the iPhone:** audio and Apple Health data. No tool returns either.
- **What relay and discovery servers see:** public endpoint ids, IP addresses, times and volume; never content. Someone watching your network sees the same metadata plus the protocol name (`evoca/link/1`), which QUIC does not hide.
- **What happens on the computer:** whatever your agent reads is also seen by its AI model (Claude, Codex…) and stays on that computer, for example in the agent's history. Transcripts are automatic and can be wrong, and they can contain anything people said near the iPhone, including sentences that sound like instructions: the MCP server tells the model to treat them as data, never as orders.
- **Disconnecting:** on the iPhone (Settings › Your Agents › the computer › Disconnect This Computer) or on the computer (`evoca-link unpair`). It stops new reads at once; what an agent already read stays where it is.

Evoca says the same on its pairing screen, and under Settings › Your Agents › the computer, "What It Asked" lists what that computer has asked since you opened Evoca.

## Requirements

- **[uv](https://docs.astral.sh/uv/getting-started/installation/).** It installs Python 3.11 or later if you do not have it, and the dependencies the script declares (`iroh` 1.1.0, `mcp` 2.x, `segno`).
- **Evoca 1.2.1 or later on the iPhone**, the first version with Evoca Link (Settings › Your Agents).
- **A platform with an official `iroh` wheel on PyPI:** macOS 11 or later on Apple Silicon, Linux x86_64 or aarch64 with glibc 2.28 or later (`manylinux_2_28`), and Windows x64. There is no wheel and no source package for Intel Macs, Windows on ARM or musl-based Linux such as Alpine, so the program cannot run there.
- The iPhone and the computer must reach each other directly (same Wi-Fi or VPN) or both have internet access (relay).

It has been tested on macOS (Apple Silicon). The code handles Windows and Linux too, and iroh ships wheels for them, but they have not been tested yet.

## Install and pair

There is nothing to clone. With uv, one line pairs the iPhone:

```bash
uvx --from git+https://github.com/jmt-es/evoca-link evoca-link pair
```

The first run lets uv fetch evoca-link and its dependencies; later runs start at once. `uvx --refresh …` brings the latest version.

To type `evoca-link pair` exactly as Evoca's screens say, install the command once (`uv tool upgrade evoca-link` updates it):

```bash
uv tool install git+https://github.com/jmt-es/evoca-link
```

To read or change the code, run it from a clone instead: `git clone https://github.com/jmt-es/evoca-link.git`, then `uv run --script evoca_link.py pair` inside it (the script imports the `link_*.py` files next to it, so download the whole repository, not `evoca_link.py` alone).

`pair` starts the daemon if it is not running and prints a QR code, a six-digit **safety code** (Evoca calls it Security Code) and the same pairing code as text. The code lasts 5 minutes (`--ttl SECONDS` changes it, from 30 s to 15 min) and works for one iPhone only.

1. On the iPhone: Evoca › Settings › Your Agents › **Connect to a Computer** (Ajustes › Tus agentes › Conectar con un ordenador, in Spanish). Scan the QR code, or copy the text from the terminal and tap **Paste Code** (with Universal Clipboard it arrives by itself). The first time, iOS asks for access to the camera, Face ID and the local network.
2. The iPhone shows the computer's name and a Security Code. Check that it is the one in the terminal, tap **Connect** and confirm with Face ID or the iPhone passcode.
3. The terminal says "Paired with …" and prints the commands to connect your agents.

From then on there is nothing else to do: every time you open Evoca the link opens by itself ("Active link with …", top left of Today) and it closes when you leave Evoca.

## Connect your agents

`pair` prints these commands for the way you ran it.

Claude Code:

```bash
claude mcp add --scope user evoca-link -- uvx --from git+https://github.com/jmt-es/evoca-link evoca-link mcp
```

Codex:

```bash
codex mcp add evoca-link -- uvx --from git+https://github.com/jmt-es/evoca-link evoca-link mcp
```

or in `~/.codex/config.toml`:

```toml
[mcp_servers.evoca-link]
command = "uvx"
args = ["--from", "git+https://github.com/jmt-es/evoca-link", "evoca-link", "mcp"]
```

Claude Desktop and any other MCP client that starts stdio servers:

```json
{
  "mcpServers": {
    "evoca-link": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/jmt-es/evoca-link", "evoca-link", "mcp"]
    }
  }
}
```

With `uv tool install`, the command is just `evoca-link mcp`; from a clone, `uv run --quiet --script /path/to/evoca-link/evoca_link.py mcp`. If an app cannot find `uvx` or `evoca-link` because it does not inherit your shell's `PATH` (Claude Desktop, for one), put the absolute path as the command (`which uvx`, or `where uvx` on Windows). Several agents at once share the same daemon and the same link. In Claude Code, `/mcp` lists `evoca-link` once it is connected.

## MCP tools

| Tool | What it returns |
|---|---|
| `status()` | Whether the iPhone is connected (and how: direct or relay), the paired iPhones and, when connected, what the iPhone holds: days, today's minutes, fragments waiting for transcription, whether the watch or the iPhone is recording and, in `recording.on_watch`, what the watch has recorded and still holds for its next batch. It does not fail when the iPhone is away: it says so. |
| `list_days(limit)` | Days with minutes, phrases (without what played on a screen), moments, the headline of the recap and named places |
| `read_day(day, offset)` | A day as Markdown (the same as Evoca's export), in chunks of 40,000 characters |
| `search(query, since, until, limit, include_on_screen)` | The iPhone's search by words and by meaning, with who is speaking, where, and the phrase before and after |
| `moments(since, until)` | Moments you marked, with title, note, place and what was said 45 s around |
| `recent(hours, include_on_screen)` | What was said in the last hours (72 at most) |
| `transcript_since(minutes, wait_seconds, include_on_screen)` | "What was just said?": the window of Rewind. If there is audio waiting for transcription, the iPhone transcribes it first and waits up to `wait_seconds`. `coverage` says which minutes were not recorded (`missing`), which ones the watch recorded and still holds for its next batch (`on_watch`: neither a gap nor a stop; the expected time is in `recording.on_watch.due_at`) and which are still being transcribed |
| `day_recap(day)` | The recap of the day that the iPhone wrote, the conversations with title and notes, and the places |

Day arguments take `today` (or `hoy`), `yesterday` (or `ayer`) or `YYYY-MM-DD`.

With the iPhone out of reach (Evoca closed or in the background), every tool answers with a clear error: open Evoca on the iPhone. If an answer takes longer than its deadline (30 s; for `transcript_since`, 30 s plus the wait), the bridge asks the iPhone over another stream. If it answers, the link stays and the tool says to narrow the request; if not, the bridge closes that link and later calls say at once that it is not connected.

Demo data never passes for yours. If the iPhone on the link says it runs Evoca in demo mode or in a simulator, or one of its answers carries a recap signed by the demo engine («Demostración · v…» or «Demo · v…»), the tools answer with an error instead of the data for as long as that link lasts, and `status` marks the iPhone with `demo` or `simulator`. Starting the bridge with `EVOCA_LINK_ALLOW_DEMO=1` lets demo data through, for tests.

## Other commands

```bash
evoca-link status                           # bridge, paired iPhones, link (direct or relay, RTT)
evoca-link call search '{"query": "monday"}'  # one raw call, for debugging
evoca-link unpair "iPhone"                  # forget a paired iPhone on this computer
evoca-link stop                             # stop the daemon (mcp and pair start it again)
evoca-link daemon --no-relay                # run the daemon yourself, direct connections only
uv run --script mcp_smoke.py                # from a clone: a real MCP client calling every tool (Evoca open on the iPhone)
```

Without `uv tool install`, put `uvx --from git+https://github.com/jmt-es/evoca-link` before `evoca-link`; from a clone, `uv run --script evoca_link.py` instead of `evoca-link`.

The daemon that `pair` and `mcp` start by themselves uses n0's public relay and discovery. To keep your computer away from them, run `stop` if a daemon is running, then start `daemon --no-relay` and leave it running: the other commands reuse it, and the pairing code then carries only direct addresses (same network or VPN). The iPhone always uses n0's relay and discovery on its side.

From the iPhone, Settings › Your Agents › the computer › **Disconnect This Computer** revokes it: if the link is open, the computer forgets the iPhone at once; if not, the iPhone no longer calls it. "Delete All Data" in Evoca also disconnects every computer. After deleting and reinstalling Evoca, the first launch of the new install forgets the paired computers and the link identity (the Keychain outlives the app, and otherwise the iPhone would keep calling the old computer): pair again, and the computer keeps listing the old iPhone until you forget it with `unpair`.

## Where it keeps things

`~/Library/Application Support/Evoca Link` (macOS), `%APPDATA%\Evoca Link` (Windows) or `~/.config/evoca-link` (Linux); `EVOCA_LINK_HOME` changes it. There are the computer's key (`computer.key`, mode 0600), the paired iPhones (`phones.json`), the daemon's port and token (`daemon.json`, 127.0.0.1 only), `daemon.log` (no Evoca content: only method names, durations and link state; it rotates at 1 MB) and `daemon.out` (whatever the daemon prints when it starts or falls). On the iPhone, the identity and the trusted computers live in the Keychain (`AfterFirstUnlockThisDeviceOnly`, not synced).

The daemon listens on 127.0.0.1 only and every request needs the random token of `daemon.json`: anyone who can read that file (your user) can ask the iPhone while the link is live.

## Troubleshooting

- **"The iPhone is not connected."** Open Evoca on the iPhone and keep it on screen: the link opens by itself within seconds and closes when the app goes to the background. `evoca_link.py status` shows whether the bridge runs and how the link is routed.
- **The iPhone cannot find the computer while pairing.** Keep `pair` running (it says "Waiting for the iPhone…") and check that the computer is online. On guest or isolated Wi-Fi the devices cannot see each other directly, so both need internet access for the relay.
- **"The evoca-link daemon did not start."** Read `daemon.log` and `daemon.out` in the folder above.
- **uv cannot install `iroh`.** The platform has no wheel: see Requirements.

## Protocol (v1)

- **QR**: `EVOCALINK1:` + base32 of `version(1) · computer id(32) · one-time secret(32) · expiry(u32) · system(1) · name · relay URL · direct addresses`, with the name, the relay URL and each address length-prefixed and the addresses counted. It fits the alphanumeric mode of QR codes.
- **Safety code**: `u32_be(HMAC-SHA256(secret, "evoca-link/safety/v1" ‖ 0 ‖ computer id)[:4]) mod 10⁶`. A QR from another computer gives another code.
- **Pairing** (ALPN `evoca/pair/1`): the iPhone dials the id in the QR (iroh's TLS authenticates the computer) and sends `proof = HMAC(secret, "evoca-link/pair/v1" ‖ 0 ‖ computer id ‖ iPhone id ‖ len(name) ‖ name)`. The computer stores the iPhone's id (authenticated by TLS) and answers `HMAC(secret, "evoca-link/paired/v1" ‖ 0 ‖ computer id ‖ iPhone id)`, which the iPhone checks before it saves the trust. The computer remembers the secret it used: if another iPhone proves it knows it, the computer forgets the first one too and answers `crowded` to both. The same iPhone repeating the attempt gets the confirmation again.
- **Link** (ALPN `evoca/link/1`): the iPhone opens a stream with `hello`; the computer answers `welcome` (or `denied` if it no longer recognizes it). After that, every question is a bidirectional stream with one frame each way: 4 bytes of big-endian length + UTF-8 JSON (`{"v":1,"id":…,"method":…,"params":…}` → `{"v":1,"id":…,"ok":…,"as_of":…,"result"|"error":…}`). Revoking is a stream with `{"type":"revoke"}`. The iPhone reads at most 1 MB per frame and answers three questions at a time (the others wait); `ping` lets the computer know whether the iPhone is still there when an answer is late.
- `fixtures/link-vectors.json` holds conformance vectors for the QR payload, the proofs and the frames. `test_link_protocol.py` runs them against this implementation, and they also pass against Evoca's Swift implementation.

## Tests

```bash
uv run python -m unittest -v
```

uv installs the dependencies that `pyproject.toml` declares, at the versions of `uv.lock`. `test_link_protocol` checks the QR, the codes, the proofs and the frames, and needs no dependencies: `python3 -m unittest -v test_link_protocol` runs it alone. `test_link_bridge` pairs, links, queries and revokes a simulated iPhone written in Python over real iroh (127.0.0.1, no relay). `test_link_mcp` calls the tools with a real MCP client through the daemon.

## Limits of this version

- It answers only while Evoca is open on the iPhone. Answering with the iPhone in your pocket would need an encrypted copy of the archive on the computer, which does not exist.
- By default it uses n0's public relays and discovery (they see ids, IP addresses and times, never content).
- Tested end to end with the iOS simulator and with a real iPhone, always with a Mac as the computer. The relay path, NAT traversal and cellular networks have not been measured yet.
- There is no short code to pair without a QR or the clipboard; that needs a PAKE.
- One computer talks to one iPhone at a time: with two paired and both open, the one that connected last answers.
- What an agent reads reaches its AI model in clear text and stays on the computer.

## License

[MIT](LICENSE) © 2026 Javier Martínez Tornero.

---

## Español

Este es el lado del ordenador de **Evoca Link**: un enlace en vivo, cifrado de extremo a extremo, entre [Evoca](https://evoca.day) en tu iPhone y tu ordenador. Mientras Evoca está abierta en el iPhone, Claude Code, Codex o cualquier otro cliente MCP de tu ordenador le pregunta directamente al iPhone: qué guarda, qué días hay, un día entero, una búsqueda (por palabras y por significado), momentos marcados, lo dicho hace poco, «¿qué se dijo en los últimos N minutos?» y el resumen de un día. Solo lectura.

Evoca es una grabadora de todo el día para Apple Watch que transcribe en el iPhone. Este repositorio es todo lo que corre en el ordenador: un script de [uv](https://docs.astral.sh/uv/) con el comando de emparejamiento, un pequeño daemon en segundo plano que mantiene el enlace y un servidor MCP. El lado del iPhone va dentro de la app Evoca.

- **El iPhone llama al ordenador.** iOS no deja que una app atienda conexiones suspendida: Evoca marca al ordenador al abrirse y cuelga al pasar a segundo plano. El ordenador espera con el daemon (el puente), que mantiene el enlace y lo comparte entre todos tus agentes.
- **El iPhone responde.** Cada respuesta la calcula el iPhone con su propio archivo (la misma búsqueda híbrida de Buscar, el mismo tramo de Rebobinar) y lleva `as_of`, la hora del iPhone al responder.

### Qué envía y qué no

- **Punto a punto y cifrado de extremo a extremo.** Los dos extremos son el iPhone y tu ordenador, emparejados por sus claves Ed25519. El transporte es [iroh](https://iroh.computer) 1.x: QUIC con TLS 1.3 entre esas dos claves. Va directo cuando la red lo permite (misma wifi, o atravesando NAT) y, si no, por un relé público de n0, la empresa de iroh, que reenvía datos que no puede leer.
- **Nada pasa por servidores de Evoca**, porque Evoca no tiene servidores ni cuentas. Aparte del propio enlace (y de los servicios de relé y descubrimiento que usa iroh), el programa no contacta con nada: ni telemetría, ni analíticas, ni comprobación de actualizaciones (uv descarga las dependencias la primera vez).
- **Lo que puede leer un agente**, con Evoca abierta y solo después de que emparejes el ordenador: transcripciones, momentos marcados, lugares con nombre y resúmenes del día, y el estado del archivo (días, minutos grabados, fragmentos sin transcribir, si graban el reloj o el iPhone). Es solo lectura: ninguna herramienta cambia nada en el iPhone.
- **Lo que nunca sale del iPhone:** el audio y los datos de Salud de Apple. Ninguna herramienta devuelve ninguno de los dos.
- **Lo que ven el relé y el descubrimiento:** ids públicos, direcciones IP, horas y volumen; nunca contenido. Quien mire tu red ve esos mismos metadatos y el nombre del protocolo (`evoca/link/1`), que QUIC no oculta.
- **Lo que pasa en el ordenador:** lo que lea tu agente lo verá también su modelo de IA (Claude, Codex…) y quedará en ese ordenador, por ejemplo en el historial del agente. Las transcripciones son automáticas, pueden fallar y pueden contener cualquier cosa que se dijera cerca del iPhone, incluidas frases que suenan a instrucciones: el servidor MCP le dice al modelo que las trate como datos, nunca como órdenes.
- **Desconectar:** en el iPhone (Ajustes › Tus agentes › el ordenador › Desconectar este ordenador) o en el ordenador (`evoca-link unpair`). Corta las lecturas nuevas al momento; lo que un agente ya leyó se queda donde está.

Evoca dice lo mismo en su hoja de emparejamiento y, en Ajustes › Tus agentes › el ordenador, «Lo que ha consultado» lista lo que ha preguntado ese ordenador desde que abriste Evoca.

### Requisitos

- **[uv](https://docs.astral.sh/uv/getting-started/installation/).** Instala Python 3.11 o posterior si no lo tienes y las dependencias que declara el script (`iroh` 1.1.0, `mcp` 2.x, `segno`).
- **Evoca 1.2.1 o posterior en el iPhone**, la primera versión con Evoca Link (Ajustes › Tus agentes).
- **Una plataforma con rueda oficial de `iroh` en PyPI:** macOS 11 o posterior con Apple Silicon, Linux x86_64 o aarch64 con glibc 2.28 o posterior (`manylinux_2_28`) y Windows x64. No hay rueda ni paquete de código fuente para Mac Intel, Windows en ARM ni Linux con musl (Alpine), así que ahí no funciona.
- El iPhone y el ordenador deben alcanzarse directamente (misma wifi o VPN) o tener los dos acceso a internet (relé).

Está probado en macOS (Apple Silicon). El código contempla también Windows y Linux, y iroh publica ruedas para ellos, pero todavía no se han probado.

### Instalar y emparejar

No hay nada que clonar. Con uv, una línea empareja el iPhone:

```bash
uvx --from git+https://github.com/jmt-es/evoca-link evoca-link pair
```

La primera vez uv descarga evoca-link y sus dependencias; las siguientes arranca al momento. `uvx --refresh …` trae la última versión.

Para escribir `evoca-link pair` tal cual lo dicen las pantallas de Evoca, instala el comando una vez (`uv tool upgrade evoca-link` lo actualiza):

```bash
uv tool install git+https://github.com/jmt-es/evoca-link
```

Para leer o cambiar el código, úsalo desde una copia: `git clone https://github.com/jmt-es/evoca-link.git` y, dentro, `uv run --script evoca_link.py pair` (el script importa los `link_*.py` que tiene al lado, así que baja el repositorio entero, no solo `evoca_link.py`).

`pair` arranca el daemon si no está en marcha y pinta un QR, un **código de seguridad** de seis dígitos y el mismo código de emparejamiento en texto. Dura 5 minutos (`--ttl SEGUNDOS` lo cambia, de 30 s a 15 min) y sirve para un solo iPhone.

1. En el iPhone: Evoca › Ajustes › Tus agentes › **Conectar con un ordenador** (Settings › Your Agents › Connect to a Computer, en inglés). Escanea el QR, o copia el texto de la terminal y toca **Pegar código** (con Portapapeles universal llega solo). La primera vez, iOS pide acceso a la cámara, a Face ID y a la red local.
2. El iPhone muestra el nombre del ordenador y un código de seguridad. Comprueba que es el de la terminal, toca **Conectar** y confirma con Face ID o el código del iPhone.
3. La terminal dice «Emparejado con …» y te da los comandos para conectar tus agentes.

Desde ahí no hay que hacer nada más: cada vez que abres Evoca el enlace se abre solo («Enlace activo con …», arriba a la izquierda en Hoy) y se cierra al salir de Evoca.

### Conectar los agentes

`pair` imprime estos comandos según cómo lo hayas ejecutado.

Claude Code:

```bash
claude mcp add --scope user evoca-link -- uvx --from git+https://github.com/jmt-es/evoca-link evoca-link mcp
```

Codex:

```bash
codex mcp add evoca-link -- uvx --from git+https://github.com/jmt-es/evoca-link evoca-link mcp
```

o en `~/.codex/config.toml`:

```toml
[mcp_servers.evoca-link]
command = "uvx"
args = ["--from", "git+https://github.com/jmt-es/evoca-link", "evoca-link", "mcp"]
```

Claude Desktop y cualquier otro cliente MCP que arranque servidores por stdio usan el mismo `command` y `args` en su configuración (el bloque JSON de arriba). Con `uv tool install` el comando es solo `evoca-link mcp`; desde una copia, `uv run --quiet --script /ruta/a/evoca-link/evoca_link.py mcp`. Si una app no encuentra `uvx` o `evoca-link` porque no hereda el `PATH` de tu shell (Claude Desktop, por ejemplo), pon la ruta absoluta como comando (`which uvx`, o `where uvx` en Windows). Varios agentes a la vez comparten el mismo daemon y el mismo enlace. En Claude Code, `/mcp` lista `evoca-link` cuando está conectado.

### Herramientas MCP

`status`, `list_days`, `read_day`, `search`, `moments`, `recent`, `transcript_since` y `day_recap`, las mismas que en la tabla de arriba: estado del iPhone y de lo que guarda, días, un día entero en Markdown, búsqueda por palabras y por significado, momentos, lo dicho en las últimas horas, «¿qué se dijo?» (el tramo de Rebobinar, con qué minutos faltan, cuáles guarda el reloj para su próximo envío y cuáles siguen transcribiéndose) y el resumen del día. Los días se piden con `hoy` (o `today`), `ayer` (o `yesterday`) o `YYYY-MM-DD`.

Con el iPhone fuera de alcance (Evoca cerrada o en segundo plano), cada herramienta responde con un error claro: abre Evoca en el iPhone.

Los datos de demostración nunca pasan por tuyos. Si el iPhone del enlace dice que Evoca está en modo demostración o en un simulador, o una de sus respuestas trae un resumen firmado por el motor de demostración («Demostración · v…» o «Demo · v…»), las herramientas responden con un error en vez de con los datos mientras dure ese enlace, y `status` marca el iPhone con `demo` o `simulator`. Arrancar el puente con `EVOCA_LINK_ALLOW_DEMO=1` los deja pasar, para pruebas.

### Otros comandos, datos locales y problemas

Los comandos (`evoca-link status`, `call`, `unpair`, `stop`, `daemon --no-relay` y, desde una copia, `mcp_smoke.py`) y la sección de problemas frecuentes están arriba, en inglés. Sin `uv tool install`, pon `uvx --from git+https://github.com/jmt-es/evoca-link` delante de `evoca-link`. El daemon que arrancan solos `pair` y `mcp` usa el relé y el descubrimiento públicos de n0; para que tu ordenador no los use, ejecuta `stop`, arranca `daemon --no-relay` y déjalo en marcha: el código de emparejamiento llevará solo direcciones directas (misma red o VPN). El iPhone usa siempre el relé y el descubrimiento de n0 por su parte.

Desde el iPhone, Ajustes › Tus agentes › el ordenador › **Desconectar este ordenador** lo revoca. Lo que un agente ya leyó se queda en ese ordenador. «Borrar todos los datos» también desconecta todos los ordenadores. Al borrar Evoca y volver a instalarla, el primer arranque de la nueva instalación olvida los ordenadores emparejados y la identidad del enlace: hay que emparejar otra vez, y el ordenador sigue listando el iPhone de antes hasta que lo olvides con `unpair`.

El ordenador guarda sus datos en `~/Library/Application Support/Evoca Link` (macOS), `%APPDATA%\Evoca Link` (Windows) o `~/.config/evoca-link` (Linux); `EVOCA_LINK_HOME` lo cambia. Ahí están la clave del ordenador (`computer.key`, modo 0600), los iPhone emparejados (`phones.json`), el puerto y el token del daemon (`daemon.json`, solo 127.0.0.1), `daemon.log` (sin contenido de Evoca: solo nombres de método, duraciones y estado del enlace; rota a 1 MB) y `daemon.out`. En el iPhone, la identidad y los ordenadores de confianza van en el Llavero (`AfterFirstUnlockThisDeviceOnly`, sin sincronizar).

### Límites de esta versión

- Solo responde con Evoca abierta en el iPhone. Para responder con el iPhone en el bolsillo haría falta una copia cifrada del archivo en el ordenador, que no existe.
- Por defecto usa los relés y el descubrimiento públicos de n0 (ven ids, IP y horas, nunca contenido).
- Probado de punta a punta con el simulador de iOS y con un iPhone real, siempre con un Mac como ordenador. Falta medir el relé, el paso por NAT y las redes móviles.
- No hay código corto para emparejar sin QR ni portapapeles; pide un PAKE.
- Un ordenador habla con un iPhone a la vez: con dos emparejados y abiertos, responde el último que se conectó.
- Lo que lee un agente llega en claro a su modelo de IA y queda en el ordenador.

El protocolo y los tests están descritos arriba, en inglés. La licencia es [MIT](LICENSE).
