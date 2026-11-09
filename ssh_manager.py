"""
SSH Manager — single-file entry point.
  No args  →  launch the GUI
  connect <name>  →  open interactive SSH session in this console window
"""
import sys
import os

# ── Paths ──────────────────────────────────────────────────────────────────────

if getattr(sys, "frozen", False):          # running as PyInstaller .exe
    BASE_DIR = os.path.dirname(sys.executable)
else:                                       # running as .py script
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "servers.json")
if not os.path.exists(CONFIG_FILE):
    # exe built into dist\ : share the project's servers.json one level up
    _parent = os.path.join(os.path.dirname(BASE_DIR), "servers.json")
    if os.path.exists(_parent):
        CONFIG_FILE = _parent


# ── Config helpers ─────────────────────────────────────────────────────────────

def load_config():
    import json
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"servers": []}


def save_config(config):
    import json
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
#  SSH CONNECT MODE  (runs inside the new console window)
# ══════════════════════════════════════════════════════════════════════════════

def _enable_vt_mode():
    """Enable ANSI/VT escape processing for the current console."""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 0x0001 | 0x0002 | 0x0004)  # stdout VT
        k.SetConsoleMode(k.GetStdHandle(-10), 0x0200)                     # stdin VT
    except Exception:
        pass


def _term_size():
    """Current local console size as (columns, rows)."""
    try:
        size = os.get_terminal_size()
        return size.columns, size.lines
    except OSError:
        return 120, 30


def _drain(channel, timeout=2.0):
    import time
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if channel.recv_ready():
            chunk = channel.recv(4096)
            if chunk:
                buf += chunk
                deadline = time.time() + 0.4
        else:
            time.sleep(0.05)
    return buf


def _wait_for_prompt(channel, markers, timeout=10.0):
    import time
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


def _interactive_session(channel):
    """Bidirectional terminal loop: keyboard → SSH, SSH output → stdout."""
    import msvcrt, threading, time

    stop = threading.Event()

    def _reader():
        while not stop.is_set():
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
        stop.set()

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    SPECIAL = {
        "H": b"\x1b[A", "P": b"\x1b[B", "K": b"\x1b[D", "M": b"\x1b[C",
        "G": b"\x1b[H", "O": b"\x1b[F", "I": b"\x1b[5~", "Q": b"\x1b[6~",
        "S": b"\x1b[3~", "R": b"\x1b[2~",
    }

    last_size = _term_size()
    pending_high = ""   # high surrogate waiting for its low half

    try:
        while not stop.is_set():
            # Propagate window resizes so full-screen apps redraw correctly
            size = _term_size()
            if size != last_size:
                last_size = size
                try:
                    channel.resize_pty(width=size[0], height=size[1])
                except Exception:
                    pass

            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    seq = SPECIAL.get(msvcrt.getwch())
                    if seq:
                        channel.send(seq)
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
        stop.set()
        reader.join(timeout=2)


def _share_remote_dir(server, as_is=False):
    """Server-side directory for the Share Path, or "" when none is set.

    The first path segment is the SMB share name (it maps to /), so
    "/share/root/one_software" becomes "/root/one_software". With as_is the
    path is returned untouched, for servers that store the real path.
    """
    path = (server.get("share_path") or "").strip().replace("\\", "/")
    if not path:
        return ""
    if path.startswith("//"):                   # //host/share/...: drop host
        path = "/" + path[2:].partition("/")[2]
    parts = [p for p in path.split("/") if p]
    if as_is:
        return "/" + "/".join(parts)
    return "/" + "/".join(parts[1:])


def run_connect_mode(server_name: str):
    """Called when the exe is invoked with 'connect <name>'."""
    import time

    server = next(
        (s for s in load_config().get("servers", []) if s["name"] == server_name),
        None,
    )
    if not server:
        print(f"[ERROR] Server '{server_name}' not found in {CONFIG_FILE}")
        input("\nPress Enter to exit...")
        sys.exit(1)

    _enable_vt_mode()

    host          = server.get("host", "")
    port          = int(server.get("port", 22) or 22)
    username      = server.get("username", "")
    password      = server.get("password", "")
    switch_password = server.get("sudo_password", password)
    # backward-compat: old configs used sudo_su=true boolean
    switch_user = server.get("switch_user",
                             "sudo_su" if server.get("sudo_su") else "none")

    print(f"\n  SSH Manager — connecting to {username}@{host}:{port} …\n")

    try:
        import paramiko
    except ImportError:
        print("[ERROR] paramiko not installed. Run:  pip install paramiko")
        input("\nPress Enter to exit...")
        sys.exit(1)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            host, port=port, username=username, password=password,
            timeout=15, look_for_keys=False, allow_agent=False,
        )
    except paramiko.AuthenticationException:
        print(f"[ERROR] Authentication failed for {username}@{host}")
        input("\nPress Enter to exit...")
        sys.exit(1)
    except Exception as exc:
        print(f"[ERROR] Could not connect: {exc}")
        input("\nPress Enter to exit...")
        sys.exit(1)

    print("  Connected!\n")
    cols, rows = _term_size()
    channel = client.invoke_shell(term="xterm-256color", width=cols, height=rows)
    channel.settimeout(0.0)

    banner = _drain(channel, timeout=2.0)
    if banner:
        sys.stdout.buffer.write(banner)
        sys.stdout.buffer.flush()

    if switch_user == "sudo_su":
        channel.send(b"sudo su\n")
        output = _wait_for_prompt(channel, ["password", "[sudo]"], timeout=10.0)
        if output:
            sys.stdout.buffer.write(output)
            sys.stdout.buffer.flush()
        if any(m in output.decode("utf-8", errors="replace").lower()
               for m in ["password", "[sudo]"]):
            channel.send((switch_password + "\n").encode())
            time.sleep(0.5)
            result = _drain(channel, timeout=2.0)
            if result:
                sys.stdout.buffer.write(result)
                sys.stdout.buffer.flush()
        else:
            print("\n[WARN] sudo password prompt not detected.")

    elif switch_user == "su_root":
        channel.send(b"su root\n")
        output = _wait_for_prompt(channel, ["password", "Password"], timeout=10.0)
        if output:
            sys.stdout.buffer.write(output)
            sys.stdout.buffer.flush()
        if "password" in output.decode("utf-8", errors="replace").lower():
            channel.send((switch_password + "\n").encode())
            time.sleep(0.5)
            result = _drain(channel, timeout=2.0)
            if result:
                sys.stdout.buffer.write(result)
                sys.stdout.buffer.flush()
        else:
            print("\n[WARN] su root password prompt not detected.")

    remote_dir = _share_remote_dir(server)
    if remote_dir:
        q = lambda d: "'" + d.replace("'", "'\\''") + "'"
        cmd = f"cd {q(remote_dir)}"
        literal = _share_remote_dir(server, as_is=True)
        if literal != remote_dir:       # share name may not be part of the path
            cmd += f" 2>/dev/null || cd {q(literal)}"
        channel.send((cmd + "\n").encode())
        result = _drain(channel, timeout=1.0)
        if result:
            sys.stdout.buffer.write(result)
            sys.stdout.buffer.flush()

    _interactive_session(channel)

    print("\n\n  Connection closed.")
    try:
        client.close()
    except Exception:
        pass
    input("  Press Enter to exit...")


# ══════════════════════════════════════════════════════════════════════════════
#  GUI MODE
# ══════════════════════════════════════════════════════════════════════════════

def _hide_console():
    """Hide the console window; only the tkinter GUI remains visible."""
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def _find_winscp():
    """Return the path to WinSCP.exe, or None if it can't be found."""
    import shutil
    found = shutil.which("WinSCP.exe")
    if found:
        return found
    for base in (os.environ.get("LOCALAPPDATA", "") + r"\Programs",
                 os.environ.get("ProgramFiles", ""),
                 os.environ.get("ProgramFiles(x86)", "")):
        path = os.path.join(base, "WinSCP", "WinSCP.exe")
        if base and os.path.exists(path):
            return path
    return None


def _find_workbench():
    """Return the path to MySQLWorkbench.exe, or None if it can't be found."""
    import shutil, glob
    found = shutil.which("MySQLWorkbench.exe")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles", ""),
                 os.environ.get("ProgramFiles(x86)", "")):
        hits = glob.glob(os.path.join(base, "MySQL", "MySQL Workbench*",
                                      "MySQLWorkbench.exe"))
        if base and hits:
            return hits[0]
    return None


def _upsert_workbench_connection(conn_name, local_port, db_user):
    """Create/update a Workbench connection pointing at the local tunnel port."""
    import uuid
    import xml.etree.ElementTree as ET
    path = os.path.join(os.environ["APPDATA"], "MySQL", "Workbench",
                        "connections.xml")
    tree = ET.parse(path)
    root = tree.getroot()
    lst = root.find("value[@content-struct-name='db.mgmt.Connection']")
    if lst is None:
        raise RuntimeError("Unrecognised connections.xml layout")

    owner = None
    for conn in list(lst):
        n = conn.find("value[@key='name']")
        o = conn.find("link[@key='owner']")
        if o is not None and owner is None:
            owner = o.text
        if n is not None and n.text == conn_name:
            p = conn.find("value[@key='parameterValues']")
            same = (p is not None
                    and p.findtext("value[@key='port']") == str(local_port)
                    and p.findtext("value[@key='userName']") == db_user)
            if same:
                return False        # already registered, file left untouched
            lst.remove(conn)
    if owner is None:
        raise RuntimeError("No existing Workbench connection to copy from")

    def add(parent, tag, typ, key, text="", **extra):
        e = ET.SubElement(parent, tag, {"type": typ, "key": key, **extra})
        e.text = text
        return e

    conn = ET.SubElement(lst, "value", {
        "type": "object", "struct-name": "db.mgmt.Connection",
        "id": "{" + str(uuid.uuid4()).upper() + "}",
        "struct-checksum": "0x96ba47d8"})
    add(conn, "link", "object", "driver",
        "com.mysql.rdbms.mysql.driver.native", **{"struct-name": "db.mgmt.Driver"})
    add(conn, "value", "string", "hostIdentifier",
        f"Mysql@127.0.0.1:{local_port}")
    add(conn, "value", "int", "isDefault", "0")
    ET.SubElement(conn, "value", {"type": "dict", "key": "modules"})
    params = ET.SubElement(conn, "value", {"type": "dict",
                                           "key": "parameterValues"})
    add(params, "value", "string", "hostName", "127.0.0.1")
    add(params, "value", "int", "port", str(local_port))
    add(params, "value", "string", "userName", db_user)
    add(params, "value", "string", "password", "")
    add(params, "value", "string", "schema", "")
    add(conn, "value", "string", "name", conn_name)
    add(conn, "link", "object", "owner", owner, **{"struct-name": "GrtObject"})
    ET.indent(tree)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return True


def run_tunnel_mode(server_name: str, local_port: int):
    """Hidden child process: forward 127.0.0.1:<local_port> to the server's DB
    over SSH until MySQL Workbench has been opened and closed again."""
    import socket, threading, subprocess, time
    import paramiko

    server = next(
        (s for s in load_config().get("servers", []) if s["name"] == server_name),
        None,
    )
    if not server:
        sys.exit(1)
    db_port = int(server.get("db_port", 3306) or 3306)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(server["host"], port=int(server.get("port", 22) or 22),
                   username=server.get("username", ""),
                   password=server.get("password", ""),
                   timeout=15, look_for_keys=False, allow_agent=False)
    transport = client.get_transport()
    transport.set_keepalive(30)

    def pump(a, b):
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except Exception:
            pass
        finally:
            for s in (a, b):
                try:
                    s.close()
                except Exception:
                    pass

    def serve():
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", local_port))
        listener.listen(5)
        while True:
            sock, _ = listener.accept()
            try:
                chan = transport.open_channel(
                    "direct-tcpip", ("127.0.0.1", db_port), sock.getpeername())
            except Exception:
                sock.close()
                continue
            threading.Thread(target=pump, args=(sock, chan), daemon=True).start()
            threading.Thread(target=pump, args=(chan, sock), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()

    def wb_running():
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq MySQLWorkbench.exe", "/NH"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW).stdout
        return "MySQLWorkbench.exe" in out

    deadline = time.time() + 90
    while not wb_running():
        if time.time() > deadline:
            return
        time.sleep(1)
    while wb_running() and transport.is_active():
        time.sleep(2)


def _self_launcher():
    """Return argv prefix to re-launch this program as a child process."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        # started windowless; child sessions need a console-capable python
        console = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(console):
            exe = console
    return [exe, os.path.abspath(__file__)]



def run_gui_mode():
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
    import subprocess, tempfile, json

    # ── Server Dialog ──────────────────────────────────────────────────────────

    class ServerDialog(tk.Toplevel):
        def __init__(self, parent, server=None, groups=(), default_group=""):
            super().__init__(parent)
            self.result = None
            self.title("Add Server" if server is None else "Edit Server")
            self.resizable(False, False)
            self.grab_set()
            self.transient(parent)

            fields = [
                ("Name",            "name",          False),
                ("Host / IP",       "host",          False),
                ("Port",            "port",          False),
                ("Username",        "username",      False),
                ("SSH Password",    "password",      True),
                ("Switch Password", "sudo_password", True),
                ("DB User",         "db_user",       False),
                ("DB Password",     "db_password",   True),
                ("DB Port",         "db_port",       False),
                ("Share Path",      "share_path",    False),
            ]

            self.vars = {}
            for i, (label, key, is_pw) in enumerate(fields):
                ttk.Label(self, text=label, width=16, anchor="e").grid(
                    row=i, column=0, padx=(10, 5), pady=6, sticky="e"
                )
                default = ""
                _defaults = {"port": "22", "db_port": "3306",
                             "db_user": "root", "db_password": "root"}
                if server:
                    default = str(server.get(key, _defaults.get(key, "")))
                else:
                    default = _defaults.get(key, "")
                var = tk.StringVar(value=default)
                entry = ttk.Entry(self, textvariable=var, width=30,
                                  show="*" if is_pw else "")
                entry.grid(row=i, column=1, padx=(0, 5), pady=6, sticky="w")
                if is_pw:
                    sv = tk.BooleanVar(value=False)
                    def _make(e=entry, v=sv):
                        def toggle(): e.config(show="" if v.get() else "*")
                        return toggle
                    ttk.Checkbutton(self, text="Show", variable=sv,
                                    command=_make()).grid(row=i, column=2, padx=5)
                self.vars[key] = var
                if i == 0:
                    entry.focus()

            row = len(fields)
            ttk.Label(self, text="Group:", width=16, anchor="e").grid(
                row=row, column=0, padx=(10, 5), pady=6, sticky="e"
            )
            self.group_var = tk.StringVar(
                value=server.get("group", "") if server else default_group
            )
            # editable: pick an existing group or type a new name
            ttk.Combobox(
                self, textvariable=self.group_var,
                values=[""] + list(groups), width=28,
            ).grid(row=row, column=1, columnspan=2, padx=(0, 5), pady=6, sticky="w")
            row += 1
            ttk.Label(self, text="After login:", width=16, anchor="e").grid(
                row=row, column=0, padx=(10, 5), pady=6, sticky="e"
            )
            # backward-compat: read old sudo_su bool or new switch_user string
            if server:
                _sw = server.get("switch_user",
                                 "sudo_su" if server.get("sudo_su") else "none")
            else:
                _sw = "none"
            SWITCH_OPTIONS = ["none", "sudo su", "su root"]
            _sw_display = {"none": "none", "sudo_su": "sudo su",
                           "su_root": "su root"}.get(_sw, "none")
            self.switch_var = tk.StringVar(value=_sw_display)
            ttk.Combobox(
                self, textvariable=self.switch_var,
                values=SWITCH_OPTIONS, state="readonly", width=12,
            ).grid(row=row, column=1, padx=(0, 5), pady=6, sticky="w")
            ttk.Label(self, text="(auto switch user)", foreground="gray").grid(
                row=row, column=2, padx=5, sticky="w"
            )

            bf = ttk.Frame(self)
            bf.grid(row=row + 1, column=0, columnspan=3, pady=10)
            ttk.Button(bf, text="Save",   command=self._save,   width=12).pack(side=tk.LEFT, padx=5)
            ttk.Button(bf, text="Cancel", command=self.destroy, width=12).pack(side=tk.LEFT, padx=5)

            self.bind("<Return>", lambda _: self._save())
            self.bind("<Escape>", lambda _: self.destroy())

        def _save(self):
            from tkinter import messagebox as mb
            name = self.vars["name"].get().strip()
            host = self.vars["host"].get().strip()
            if not name:
                mb.showerror("Error", "Name is required.", parent=self)
                return
            if not host:
                mb.showerror("Error", "Host / IP is required.", parent=self)
                return
            self.result = {k: v.get().strip() for k, v in self.vars.items()}
            _display_to_key = {"none": "none", "sudo su": "sudo_su",
                               "su root": "su_root"}
            self.result["switch_user"] = _display_to_key.get(
                self.switch_var.get(), "none"
            )
            group = self.group_var.get().strip()
            if group:
                self.result["group"] = group
            self.destroy()

    # ── Main Window ────────────────────────────────────────────────────────────

    class SSHManager(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("SSH Manager")
            self.geometry("720x640")
            self.minsize(560, 540)
            self.config_data = load_config()
            self._apply_font()
            self._check_paramiko()
            self._build_ui()
            self._load_all_pages()

        def _apply_font(self):
            """Use Inter for the whole UI when installed, else Segoe UI."""
            import tkinter.font as tkfont
            self.ui_font = ("Inter" if "Inter" in tkfont.families()
                            else "Segoe UI")
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                         "TkHeadingFont", "TkCaptionFont", "TkTooltipFont"):
                tkfont.nametofont(name).configure(family=self.ui_font, size=9)

        def _check_paramiko(self):
            try:
                import paramiko  # noqa: F401
            except ImportError:
                messagebox.showerror(
                    "Missing Dependency",
                    "The 'paramiko' library is not installed.\n\n"
                    "Run:  pip install paramiko\n\nThen restart.",
                )

        def _build_ui(self):
            style = ttk.Style(self)
            style.theme_use("clam")
            style.configure("TButton", padding=(10, 4))

            tb = ttk.Frame(self, padding=(5, 5))
            tb.pack(fill=tk.X)
            ttk.Button(tb, text="＋  New Server", command=self._add_server).pack(side=tk.LEFT, padx=2)
            ttk.Button(tb, text="▤  New Group",   command=self._add_group).pack(side=tk.LEFT, padx=2)
            ttk.Button(tb, text="✎  Edit",        command=self._edit_server).pack(side=tk.LEFT, padx=2)
            ttk.Button(tb, text="⧉  Duplicate",   command=self._duplicate_server).pack(side=tk.LEFT, padx=2)
            ttk.Button(tb, text="▲  Up",          command=lambda: self._move_server(-1)).pack(side=tk.LEFT, padx=2)
            ttk.Button(tb, text="▼  Down",        command=lambda: self._move_server(1)).pack(side=tk.LEFT, padx=2)
            ttk.Button(tb, text="✕  Delete",      command=self._delete_server).pack(side=tk.LEFT, padx=2)

            ttk.Separator(self, orient="horizontal").pack(fill=tk.X)

            body = ttk.Frame(self)
            body.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

            # Vertical "tab" strip: a tree of groups and servers.
            # Drag a row to reorder it, or drop it on a group to move it in.
            side = ttk.Frame(body)
            side.pack(side=tk.LEFT, fill=tk.Y)
            style.configure("Treeview", font=(self.ui_font, 9), rowheight=26)
            self.tree = ttk.Treeview(side, show="tree", selectmode="browse",
                                     height=10)
            self.tree.column("#0", width=230, stretch=False)
            self.tree.tag_configure("group", font=(self.ui_font, 9, "bold"))
            self.tree.tag_configure("drop", background="#cfe3ff")
            sb = ttk.Scrollbar(side, orient="vertical",
                               command=self.tree.yview)
            self.tree.configure(yscrollcommand=sb.set)
            self.tree.pack(side=tk.LEFT, fill=tk.Y)
            sb.pack(side=tk.LEFT, fill=tk.Y)
            self.tree.bind("<<TreeviewSelect>>",
                           lambda _: self._show_page(self._current_index()))
            self.tree.bind("<ButtonPress-1>",   self._drag_start, add="+")
            self.tree.bind("<B1-Motion>",       self._drag_motion)
            self.tree.bind("<ButtonRelease-1>", self._drag_end, add="+")
            self._drag_src = None
            self._dragging = False
            self._drop_target = ""

            self.content = ttk.Frame(body)
            self.content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0))
            self.pages = []

            self._empty_label = ttk.Label(
                self,
                text='No servers yet — click  "＋ New Server"  to add one.',
                foreground="gray",
            )
            self._empty_label.pack(pady=20)

        def _load_all_pages(self):
            self._rebuild(select="s0" if self.config_data.get("servers") else None)

        # Tree item ids: "s<index>" for a server, "g:<name>" for a group.

        def _servers(self):
            return self.config_data.setdefault("servers", [])

        def _groups(self):
            groups = self.config_data.setdefault("groups", [])
            for s in self._servers():
                g = s.get("group")
                if g and g not in groups:
                    groups.append(g)
            return groups

        def _rebuild(self, select=None):
            """Rebuild the tree and every page from config_data."""
            closed = {i for i in self.tree.get_children()
                      if i.startswith("g:") and not self.tree.item(i, "open")}
            for p in self.pages:
                p.destroy()
            self.pages = []
            self.tree.delete(*self.tree.get_children())

            servers = self._servers()
            for s in servers:
                self._create_page(s)
            for i, s in enumerate(servers):
                if not s.get("group"):
                    self.tree.insert("", tk.END, iid=f"s{i}", text=s["name"])
            for g in self._groups():
                gid = f"g:{g}"
                self.tree.insert("", tk.END, iid=gid, text=g, tags=("group",),
                                 open=gid not in closed)
                for i, s in enumerate(servers):
                    if s.get("group") == g:
                        self.tree.insert(gid, tk.END, iid=f"s{i}", text=s["name"])

            if select and self.tree.exists(select):
                self._select_item(select)
            else:
                self._show_page(-1)
            self._refresh_empty()

        def _select_item(self, iid):
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
            self._show_page(self._current_index())

        def _select(self, idx):
            self._select_item(f"s{idx}")

        def _show_page(self, idx):
            for p in self.pages:
                p.pack_forget()
            if 0 <= idx < len(self.pages):
                self.pages[idx].pack(fill=tk.BOTH, expand=True)

        def _refresh_empty(self):
            if self.config_data.get("servers"):
                self._empty_label.pack_forget()
            else:
                self._empty_label.pack(pady=20)

        def _create_page(self, server):
            frame = ttk.Frame(self.content, padding=15)
            self.pages.append(frame)

            card = ttk.LabelFrame(frame, text=" Server Details ", padding=12)
            card.pack(fill=tk.X)

            _sw = server.get("switch_user",
                             "sudo_su" if server.get("sudo_su") else "none")
            _sw_label = {"none": "None", "sudo_su": "sudo su",
                         "su_root": "su root"}.get(_sw, "None")
            rows = [
                ("Name",       server.get("name", "")),
                ("Group",      server.get("group", "") or "—"),
                ("Host / IP",  server.get("host", "")),
                ("Port",       server.get("port", "22")),
                ("Username",   server.get("username", "")),
                ("After login", _sw_label),
                ("Share Path", server.get("share_path", "") or "—"),
            ]
            for i, (lbl, val) in enumerate(rows):
                ttk.Label(card, text=f"{lbl}:", width=13, anchor="e",
                          foreground="#555").grid(row=i, column=0, sticky="e",
                                                   padx=(0, 8), pady=3)
                ttk.Label(card, text=val, anchor="w",
                          font=("Consolas", 10)).grid(row=i, column=1,
                                                       sticky="w", pady=3)

            btn_area = ttk.Frame(frame)
            btn_area.pack(pady=12)
            ttk.Button(
                btn_area, text="  ▶  Connect", width=24,
                command=lambda s=server: self._connect(s),
            ).pack()
            ttk.Label(
                btn_area,
                text="Opens a new CMD window with the SSH session.",
                foreground="gray",
            ).pack(pady=(8, 0))
            ttk.Button(
                btn_area, text="  ⇄  Open in WinSCP", width=24,
                command=lambda s=server: self._open_winscp(s),
            ).pack(pady=(10, 0))
            ttk.Label(
                btn_area,
                text="Transfer files with this server's saved login.",
                foreground="gray",
            ).pack(pady=(8, 0))
            ttk.Button(
                btn_area, text="Windows", width=24,
                command=lambda s=server: self._open_share(s),
            ).pack(pady=(10, 0))
            ttk.Label(
                btn_area,
                text="Opens this server's Share Path in a Windows folder window.",
                foreground="gray",
            ).pack(pady=(8, 0))
            ttk.Button(
                btn_area, text="Workbench", width=24,
                command=lambda s=server: self._open_workbench(s),
            ).pack(pady=(10, 0))
            ttk.Label(
                btn_area,
                text="Tunnels over SSH to this server's MySQL (default root / root).",
                foreground="gray",
            ).pack(pady=(8, 0))

        def _open_share(self, server):
            path = server.get("share_path", "").strip()
            if not path:
                messagebox.showinfo(
                    "Share Path missing",
                    "Edit this server and set Share Path, e.g.\n"
                    "/share/root/one_software")
                return
            path = path.replace("/", "\\")
            if not path.startswith("\\\\"):     # no host given: use the server's
                path = f"\\\\{server.get('host', '')}\\" + path.lstrip("\\")
            try:
                subprocess.Popen(["explorer", path])
            except Exception as exc:
                messagebox.showerror("Launch Error",
                                     f"Could not open Explorer:\n{exc}")

        def _open_workbench(self, server):
            import socket
            exe = _find_workbench()
            if not exe:
                messagebox.showerror("Workbench not found",
                                     "MySQLWorkbench.exe was not found.")
                return
            server = {"db_user": "root", "db_password": "root", **{
                k: v for k, v in server.items() if v != "" or not k.startswith("db_")}}
            import time, zlib
            # fixed port per server, so the Workbench entry is made only once
            port = 20000 + zlib.crc32(server["name"].encode()) % 20000
            conn_name = f"SSHM - {server['name']}"

            def listening():
                try:
                    socket.create_connection(("127.0.0.1", port), 0.5).close()
                    return True
                except OSError:
                    return False

            try:
                changed = _upsert_workbench_connection(
                    conn_name, port, server["db_user"])
                running = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq MySQLWorkbench.exe", "/NH"],
                    capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW).stdout
                if changed and "MySQLWorkbench.exe" in running:
                    messagebox.showinfo(
                        "Workbench is running",
                        "This is the first time for this server, so Workbench "
                        "must be restarted once to see the new connection.\n"
                        "Close it and click again; later clicks won't need that.")
                    return
                if not listening():
                    subprocess.Popen(
                        _self_launcher() + ["tunnel", server["name"], str(port)],
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    for _ in range(30):     # wait for the tunnel to listen
                        if listening():
                            break
                        time.sleep(0.5)
                subprocess.Popen([exe, "--query", conn_name])
            except Exception as exc:
                messagebox.showerror("Launch Error",
                                     f"Could not start Workbench:\n{exc}")
                return
            pw = server.get("db_password", "")
            if pw:
                self.clipboard_clear()
                self.clipboard_append(pw)
                messagebox.showinfo(
                    "Workbench",
                    "The DB password is on the clipboard.\n"
                    "Paste it when Workbench asks.")

        def _open_winscp(self, server):
            from urllib.parse import quote
            exe = _find_winscp()
            if not exe:
                messagebox.showerror(
                    "WinSCP not found",
                    "WinSCP.exe was not found on PATH or in the usual "
                    "install folders.")
                return
            q = lambda v: quote(str(v), safe="")
            port = int(server.get("port", 22) or 22)
            url = (f"sftp://{q(server.get('username', ''))}:"
                   f"{q(server.get('password', ''))}"
                   f"@{server.get('host', '')}:{port}/")
            try:
                subprocess.Popen([exe, url])
            except Exception as exc:
                messagebox.showerror("Launch Error",
                                     f"Could not start WinSCP:\n{exc}")

        def _connect(self, server):
            safe_name = server["name"].replace('"', '').replace("'", "")
            launcher  = _self_launcher()

            # Write a temp .bat — avoids cmd quoting headaches
            bat = tempfile.NamedTemporaryFile(
                mode="w", suffix=".bat", delete=False, encoding="utf-8"
            )
            bat.write("@echo off\n")
            bat.write(f'title SSH - {safe_name}\n')
            # launcher is either [exe] or [python, script_path]
            cmd_parts = " ".join(f'"{p}"' for p in launcher)
            bat.write(f'{cmd_parts} connect "{safe_name}"\n')
            bat.close()

            try:
                subprocess.Popen(
                    ["cmd", "/k", bat.name],
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
            except Exception as exc:
                messagebox.showerror("Launch Error",
                                     f"Could not open terminal:\n{exc}")

        # ── CRUD ──────────────────────────────────────────────────────────────

        def _selected_item(self):
            sel = self.tree.selection()
            return sel[0] if sel else ""

        def _current_index(self):
            iid = self._selected_item()
            return int(iid[1:]) if iid.startswith("s") else -1

        def _current_group(self):
            iid = self._selected_item()
            return iid[2:] if iid.startswith("g:") else ""

        def _context_group(self):
            """Group the selection is in (or is), used to pre-fill new servers."""
            g = self._current_group()
            idx = self._current_index()
            if not g and idx >= 0:
                g = self._servers()[idx].get("group", "")
            return g

        def _save_and_rebuild(self, select=None):
            self._groups()                      # pick up groups named in servers
            save_config(self.config_data)
            self._rebuild(select)

        def _add_server(self):
            d = ServerDialog(self, groups=self._groups(),
                             default_group=self._context_group())
            self.wait_window(d)
            if not d.result:
                return
            servers = self._servers()
            if d.result["name"] in [s["name"] for s in servers]:
                messagebox.showerror("Error",
                    f"A server named '{d.result['name']}' already exists.")
                return
            servers.append(d.result)
            self._save_and_rebuild(f"s{len(servers) - 1}")

        def _add_group(self):
            name = simpledialog.askstring("New Group", "Group name:", parent=self)
            name = (name or "").strip()
            if not name:
                return
            if name in self._groups():
                messagebox.showerror("Error", f"A group named '{name}' already exists.")
                return
            self._groups().append(name)
            self._save_and_rebuild(f"g:{name}")

        def _rename_group(self, old):
            new = simpledialog.askstring("Rename Group", "Group name:",
                                         initialvalue=old, parent=self)
            new = (new or "").strip()
            if not new or new == old:
                return
            groups = self._groups()
            if new in groups:
                messagebox.showerror("Error", f"A group named '{new}' already exists.")
                return
            groups[groups.index(old)] = new
            for s in self._servers():
                if s.get("group") == old:
                    s["group"] = new
            self._save_and_rebuild(f"g:{new}")

        def _edit_server(self):
            group = self._current_group()
            if group:
                self._rename_group(group)
                return
            idx = self._current_index()
            if idx < 0:
                messagebox.showinfo("Edit", "Select a server or group first.")
                return
            servers = self._servers()
            d = ServerDialog(self, servers[idx], groups=self._groups())
            self.wait_window(d)
            if not d.result:
                return
            servers[idx] = d.result
            self._save_and_rebuild(f"s{idx}")

        def _duplicate_server(self):
            idx = self._current_index()
            if idx < 0:
                messagebox.showinfo("Duplicate", "Select a server first.")
                return
            servers = self._servers()
            copy = dict(servers[idx])
            taken = {s["name"] for s in servers}
            base = f"{copy['name']} (copy)"
            name, n = base, 2
            while name in taken:
                name = f"{base} {n}"
                n += 1
            copy["name"] = name
            servers.insert(idx + 1, copy)
            self._save_and_rebuild(f"s{idx + 1}")

        def _move_server(self, delta):
            """Move the selected server (within its group) or group one step."""
            group = self._current_group()
            if group:
                groups = self._groups()
                i = groups.index(group)
                j = i + delta
                if not 0 <= j < len(groups):
                    return
                groups[i], groups[j] = groups[j], groups[i]
                self._save_and_rebuild(f"g:{group}")
                return
            idx = self._current_index()
            if idx < 0:
                messagebox.showinfo("Move", "Select a server or group first.")
                return
            servers = self._servers()
            grp = servers[idx].get("group", "")
            peers = [i for i, s in enumerate(servers) if s.get("group", "") == grp]
            p = peers.index(idx) + delta
            if not 0 <= p < len(peers):
                return
            other = peers[p]
            servers[idx], servers[other] = servers[other], servers[idx]
            self._save_and_rebuild(f"s{other}")

        def _delete_server(self):
            servers = self._servers()
            group = self._current_group()
            if group:
                n = sum(1 for s in servers if s.get("group") == group)
                msg = f"Delete group '{group}'?"
                if n:
                    msg += f"\n\nIts {n} server(s) are kept and become ungrouped."
                if not messagebox.askyesno("Delete Group", msg):
                    return
                self._groups().remove(group)
                for s in servers:
                    if s.get("group") == group:
                        del s["group"]
                self._save_and_rebuild()
                return
            idx = self._current_index()
            if idx < 0:
                messagebox.showinfo("Delete", "Select a server or group first.")
                return
            name = servers[idx]["name"]
            if not messagebox.askyesno("Delete Server", f"Delete '{name}'?"):
                return
            servers.pop(idx)
            self._save_and_rebuild(
                f"s{min(idx, len(servers) - 1)}" if servers else None)

        # ── Drag & drop reordering ────────────────────────────────────────────

        def _mark_drop(self, iid, on):
            if not iid or not self.tree.exists(iid):
                return
            tags = self.tree.item(iid, "tags")
            if isinstance(tags, str):
                tags = tags.split()
            tags = [t for t in tags if t != "drop"]
            if on:
                tags.append("drop")
            self.tree.item(iid, tags=tags)

        def _drag_start(self, event):
            self._drag_src = self.tree.identify_row(event.y) or None
            self._dragging = False

        def _drag_motion(self, event):
            if not self._drag_src:
                return None
            self._dragging = True
            self.tree.configure(cursor="hand2")
            target = self.tree.identify_row(event.y)
            if target == self._drag_src:
                target = ""
            if target != self._drop_target:
                self._mark_drop(self._drop_target, False)
                self._mark_drop(target, True)
                self._drop_target = target
            return "break"          # keep the tree from re-selecting under the cursor

        def _drag_end(self, event):
            src, dragging = self._drag_src, self._dragging
            self._drag_src, self._dragging = None, False
            self.tree.configure(cursor="")
            self._mark_drop(self._drop_target, False)
            self._drop_target = ""
            if not (src and dragging):
                return
            target = self.tree.identify_row(event.y)
            if target == src:
                return
            inside = (0 <= event.x < self.tree.winfo_width()
                      and 0 <= event.y < self.tree.winfo_height())
            if not target and not inside:
                return              # released outside the list: cancel
            self._drop(src, target)

        def _drop(self, src, target):
            """Move `src` onto `target` ("" = empty space = end of the list)."""
            servers = self._servers()
            groups = self._groups()

            if src.startswith("g:"):
                g = src[2:]
                if target.startswith("s"):
                    t = servers[int(target[1:])].get("group", "")
                    if not t:
                        return      # ungrouped servers always stay above groups
                elif target.startswith("g:"):
                    t = target[2:]
                else:
                    t = None
                if t == g:
                    return
                i = groups.index(g)
                groups.pop(i)
                if t is None:
                    groups.append(g)
                else:
                    # dragged up -> land before the target, down -> after it
                    j = groups.index(t)
                    groups.insert(j + 1 if i <= j else j, g)
                self._save_and_rebuild(f"g:{g}")
                return

            idx = int(src[1:])
            s = servers[idx]
            tidx = None
            after = False
            if target.startswith("g:"):
                group = target[2:]
            elif target.startswith("s"):
                tidx = int(target[1:])
                group = servers[tidx].get("group", "")
                after = group == s.get("group", "") and idx < tidx
            else:
                group = ""

            servers.pop(idx)
            if group:
                s["group"] = group
            else:
                s.pop("group", None)
            if tidx is not None:
                pos = tidx - (tidx > idx) + (1 if after else 0)
            else:
                members = [i for i, x in enumerate(servers)
                           if x.get("group", "") == group]
                pos = members[-1] + 1 if members else len(servers)
            servers.insert(pos, s)
            self._save_and_rebuild(f"s{pos}")

    _hide_console()
    SSHManager().mainloop()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "connect":
        run_connect_mode(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "tunnel":
        run_tunnel_mode(sys.argv[2], int(sys.argv[3]))
    else:
        run_gui_mode()


if __name__ == "__main__":
    main()
