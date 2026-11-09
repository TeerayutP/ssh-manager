# Web-Triggered SSH Launcher — Architecture & Plan

## Goal

Add a web UI, reachable on the LAN/VPN only, that lets you click a server
name and have a real SSH session open as a **native console window on your
Windows PC** — the same visual result as using `ssh_manager.py`'s GUI today,
just triggered remotely from a browser.

Confirmed constraints:
- Access scope: LAN/VPN only, no public internet exposure.
- Session UX: native console window on the PC, not a browser-embedded
  terminal.
- Hosting: web frontend + backend run on a separate always-on LAN host —
  `192.168.102.102`, on a port of your choosing. This is deployed by running
  the repo directly on that server.

## How the existing spawn works (reused as-is)

`ssh_manager.py`'s GUI already has the exact primitive needed. `_connect()`
calls `_self_launcher()` (returns `[exe]` when frozen by PyInstaller, else
`[python, script_path]`), writes a temp `.bat` that runs
`<launcher> connect "<name>"`, and launches it with
`subprocess.Popen(["cmd","/k", bat], creationflags=CREATE_NEW_CONSOLE)`. The
CLI entry (`main()`) dispatches `connect <name>` into the full terminal loop.
"Open a native console for server X" is already a solved, factored-out
operation — the new agent just calls the same helper.

## Components

1. **Local PC agent** (`ssh_manager.py agent`, new mode) — persistent
   process on the user's Windows PC. Holds an **outbound** WebSocket
   connection to the backend, authenticates, waits for `launch` commands,
   and on each one calls the existing spawn logic. This solves the
   inbound-connectivity problem: the PC never needs an open port.
2. **Web backend** (on `192.168.102.102`) — serves the UI, accepts agent
   WebSocket connections, relays the server list and launch clicks to the
   right agent.
3. **Web frontend** — one static HTML/JS page: list of servers,
   online/offline status per agent, click = send launch.

## servers.json / credentials: stay on the PC only

Credentials (plaintext passwords, sudo passwords) should live **only** in
the PC agent's local `servers.json` — the backend never sees them. The
backend stores at most a **sanitized name list** (names + display labels,
no host/user/password), which the agent pushes to the backend on connect.
The web UI sends only a server **name**; the agent resolves name →
credentials locally and spawns `connect <name>`, which reads the same local
`servers.json`. This gives one source of truth (the PC's file, already used
by `connect`), zero config drift, and means a compromised/sniffed backend or
random LAN device can never exfiltrate credentials — only learn names. No
change to how `connect` loads the file.

**Important for deployment:** the copy of this repo run on
`192.168.102.102` is the **backend only** and must not carry real
credentials. The agent half with the real `servers.json` stays on your PC.

## Backend ↔ agent protocol

- **Transport**: agent is a WebSocket *client* to
  `ws://192.168.102.102:<port>/agent`. Outbound only.
- **Auth**: shared bearer token sent in the connect handshake (header or
  first frame). Backend rejects unknown tokens, so random LAN devices can't
  register an agent or trigger launches. Store the token in a local config
  file beside the agent (or Windows DPAPI / env var), not in `servers.json`.
- **Identity**: agent sends an `agent_id` (e.g. hostname + per-install
  UUID). Backend maps id → live socket.
- **Messages**: agent → backend `hello{agent_id, token, servers:[names]}`,
  periodic `pong`; backend → agent `launch{name}`. Backend → UI over its own
  socket/SSE: `agents` + `status`.
- **Heartbeat/reconnect**: ping/pong every ~20s; agent auto-reconnects with
  backoff on drop. Backend marks an agent offline when its socket closes and
  greys it out in the UI.

## Persistence on the PC

A **tray app** variant of the agent, launched via a Startup-folder shortcut
or Task Scheduler at logon. A Windows Service is overkill and complicates
the interactive-console spawn (services run in a non-interactive session;
the console must appear on the user's desktop). Startup shortcut / logon
task keeps it in the user's interactive session where `CREATE_NEW_CONSOLE`
works.

## Tech & packaging

- **Backend**: FastAPI + uvicorn (native WebSocket + static file serving in
  one small app). No DB needed — in-memory agent registry; the only
  "persistence" is each PC's own `servers.json`.
- **Packaging**: extend the existing PyInstaller spec with the `agent`
  entry mode — same binary, so `_self_launcher()`/`connect` keep working
  unchanged. Backend ships separately on `192.168.102.102` (plain Python, or
  its own small PyInstaller/exe).

## File / module layout

- `ssh_manager.py` — add `agent` dispatch in `main()`; optionally extract
  the `.bat` + `CREATE_NEW_CONSOLE` spawn into a small shared
  `spawn_connect(name)` so GUI and agent share one code path. Everything
  else untouched.
- `agent.py` (new) — WebSocket client, reconnect/heartbeat, tray UI, token
  config, calls `spawn_connect`.
- `server/app.py` (new) — FastAPI backend.
- `server/static/index.html` (new) — the UI.
- `agent_config.json` (new, local) — backend URL + auth token.

## Key risks / tradeoffs

- **PC asleep/offline**: click does nothing visible; UI must show the agent
  offline (from heartbeat) and disable/grey its servers. No remote wake.
- **Backend restart**: in-memory registry lost, but agents auto-reconnect
  within the backoff window and re-push their name lists — self-healing.
- **Duplicate agent identity**: two PCs with the same `agent_id` would
  collide; use a persisted per-install UUID and have the backend
  reject/replace duplicates with a warning.
- **Token storage/rotation**: single shared token is simplest for a LAN
  tool; store it out of `servers.json` and out of git. Rotating means
  editing each agent's `agent_config.json` + backend — acceptable at this
  scale; per-agent tokens are a later upgrade if needed.
- **Interactive session requirement**: the console only appears if the
  agent runs in the logged-in user's desktop session — reinforces
  tray/startup over service.
