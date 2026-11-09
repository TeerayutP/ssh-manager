"""
SSH Connect - interactive SSH terminal helper
Called by ssh_manager.py when you click Connect.
Usage: python ssh_connect.py <server_name>
"""
import sys
import os
import json
import time
import threading
import ctypes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "servers.json")


# ── Windows VT / ANSI support ─────────────────────────────────────────────────

def enable_vt_mode():
    """Enable ANSI escape processing so the terminal renders colours, etc."""
    try:
        kernel32 = ctypes.windll.kernel32
        # STD_INPUT_HANDLE = -10, STD_OUTPUT_HANDLE = -11, STD_ERROR_HANDLE = -12
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ENABLE_PROCESSED_OUTPUT            = 0x0001
        ENABLE_WRAP_AT_EOL_OUTPUT          = 0x0002
        out_mode = (ENABLE_VIRTUAL_TERMINAL_PROCESSING |
                    ENABLE_PROCESSED_OUTPUT |
                    ENABLE_WRAP_AT_EOL_OUTPUT)
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), out_mode)

        ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
        inp_mode = ENABLE_VIRTUAL_TERMINAL_INPUT
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-10), inp_mode)
    except Exception:
        pass


def term_size() -> tuple:
    """Current local console size as (columns, rows)."""
    try:
        size = os.get_terminal_size()
        return size.columns, size.lines
    except OSError:
        return 120, 30


# ── Config helpers ─────────────────────────────────────────────────────────────

def load_server(name: str) -> dict:
    if not os.path.exists(CONFIG_FILE):
        print(f"[ERROR] Config file not found: {CONFIG_FILE}")
        return {}
    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = json.load(f)
    for s in config.get("servers", []):
        if s["name"] == name:
            return s
    return {}


# ── SSH helpers ────────────────────────────────────────────────────────────────

def drain(channel, timeout: float = 2.0) -> bytes:
    """Read everything currently buffered in the channel."""
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if channel.recv_ready():
            chunk = channel.recv(4096)
            if chunk:
                buf += chunk
                deadline = time.time() + 0.4  # reset on fresh data
        else:
            time.sleep(0.05)
    return buf


def wait_for_prompt(channel, markers: list, timeout: float = 10.0) -> bytes:
    """Read until one of *markers* appears in the output (case-insensitive)."""
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if channel.recv_ready():
            chunk = channel.recv(4096)
            if chunk:
                buf += chunk
                low = buf.decode("utf-8", errors="replace").lower()
                if any(m.lower() in low for m in markers):
                    return buf
        else:
            time.sleep(0.05)
    return buf


# ── Interactive terminal loop ──────────────────────────────────────────────────

def interactive_session(channel):
    """
    Bidirectional interactive terminal using msvcrt for raw key reading.
    Works in PowerShell / Windows Terminal.
    """
    import msvcrt

    stop_event = threading.Event()

    # Thread: SSH output → stdout
    def _reader():
        while not stop_event.is_set():
            try:
                if channel.recv_ready():
                    data = channel.recv(4096)
                    if not data:
                        break
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                elif channel.closed:
                    break
            except Exception:
                break
            time.sleep(0.01)
        stop_event.set()

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    # Main thread: keyboard → SSH channel
    # Map for Windows extended key codes (0x00 / 0xE0 prefix)
    SPECIAL_KEYS = {
        "H":  b"\x1b[A",   # Up
        "P":  b"\x1b[B",   # Down
        "K":  b"\x1b[D",   # Left
        "M":  b"\x1b[C",   # Right
        "G":  b"\x1b[H",   # Home
        "O":  b"\x1b[F",   # End
        "I":  b"\x1b[5~",  # Page Up
        "Q":  b"\x1b[6~",  # Page Down
        "S":  b"\x1b[3~",  # Delete
        "R":  b"\x1b[2~",  # Insert
        "\x8b": b"\x1bOP", # F1
        "\x8c": b"\x1bOQ", # F2
        "\x8d": b"\x1bOR", # F3
        "\x8e": b"\x1bOS", # F4
    }

    last_size = term_size()
    pending_high = ""   # high surrogate waiting for its low half

    try:
        while not stop_event.is_set():
            # Propagate window resizes so full-screen apps redraw correctly
            size = term_size()
            if size != last_size:
                last_size = size
                try:
                    channel.resize_pty(width=size[0], height=size[1])
                except Exception:
                    pass

            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):   # extended key prefix
                    ch2 = msvcrt.getwch()
                    seq = SPECIAL_KEYS.get(ch2)
                    if seq:
                        channel.send(seq)
                    # else: unknown extended key, ignore
                elif "\ud800" <= ch <= "\udbff":
                    pending_high = ch
                else:
                    if pending_high:
                        ch = pending_high + ch
                        pending_high = ""
                    channel.send(ch.encode("utf-16", errors="surrogatepass")
                                 .decode("utf-16").encode("utf-8", errors="replace"))
            else:
                time.sleep(0.01)
    except KeyboardInterrupt:
        channel.send(b"\x03")
    finally:
        stop_event.set()
        reader.join(timeout=2)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python ssh_connect.py <server_name>")
        input("\nPress Enter to exit...")
        sys.exit(1)

    server_name = sys.argv[1]
    server = load_server(server_name)

    if not server:
        print(f"[ERROR] Server '{server_name}' not found in {CONFIG_FILE}")
        input("\nPress Enter to exit...")
        sys.exit(1)

    enable_vt_mode()

    host     = server.get("host", "")
    port     = int(server.get("port", 22) or 22)
    username = server.get("username", "")
    password = server.get("password", "")
    sudo_password = server.get("sudo_password", password)
    do_sudo_su    = server.get("sudo_su", False)

    print(f"\n  SSH Manager — connecting to {username}@{host}:{port} …\n")

    try:
        import paramiko
    except ImportError:
        print("[ERROR] paramiko is not installed. Run:  pip install paramiko")
        input("\nPress Enter to exit...")
        sys.exit(1)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            host,
            port=port,
            username=username,
            password=password,
            timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )
    except paramiko.AuthenticationException:
        print(f"[ERROR] Authentication failed for {username}@{host}")
        print("        Check the username / password in SSH Manager.")
        input("\nPress Enter to exit...")
        sys.exit(1)
    except Exception as exc:
        print(f"[ERROR] Could not connect: {exc}")
        input("\nPress Enter to exit...")
        sys.exit(1)

    print("  Connected!\n")

    cols, rows = term_size()
    channel = client.invoke_shell(term="xterm-256color", width=cols, height=rows)
    channel.settimeout(0.0)   # non-blocking recv

    # Drain the initial login banner / MOTD
    banner = drain(channel, timeout=2.0)
    if banner:
        sys.stdout.buffer.write(banner)
        sys.stdout.buffer.flush()

    # ── Automated sudo su ──────────────────────────────────────────
    if do_sudo_su:
        channel.send(b"sudo su\n")

        output = wait_for_prompt(
            channel,
            markers=["password", "[sudo]", "Password"],
            timeout=10.0,
        )
        if output:
            sys.stdout.buffer.write(output)
            sys.stdout.buffer.flush()

        decoded = output.decode("utf-8", errors="replace").lower()
        if "password" in decoded or "[sudo]" in decoded:
            channel.send((sudo_password + "\n").encode())
            time.sleep(0.5)
            result = drain(channel, timeout=2.0)
            if result:
                sys.stdout.buffer.write(result)
                sys.stdout.buffer.flush()
        else:
            print("\n[WARN] sudo su password prompt not detected — "
                  "check sudo_password in SSH Manager.")

    # ── Hand over to the user ──────────────────────────────────────
    interactive_session(channel)

    print("\n\n  Connection closed.")
    try:
        client.close()
    except Exception:
        pass
    input("  Press Enter to exit...")


if __name__ == "__main__":
    main()
