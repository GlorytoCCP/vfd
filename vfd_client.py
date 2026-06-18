"""
vfd_client.py — Client API for talking to the VFD display manager
===================================================================
Scripts that want to show something on the display NEVER import
futaba_vfd or touch GPIO directly. Instead they use this lightweight
client to send requests to the vfd_manager.py daemon, which arbitrates
between clients by priority and owns the real hardware connection.

Basic usage:

    from vfd_client import VFDClient

    # Higher number = higher priority. Whoever has the highest active
    # priority claim is what actually shows on the screen.
    client = VFDClient("sync_music", priority=10)

    client.write_str(0, "SYNCING ")
    client.progress_bar(0.5)
    ...
    client.release()   # give the display back to whoever was showing before

Or as a context manager (auto-releases when done):

    with VFDClient("sync_music", priority=10) as client:
        client.write_str(0, "SYNCING ")
        client.progress_bar(0.5)
    # automatically released here

Suggested priority levels (just a convention — pick whatever scheme fits):
    0   - idle / clock / background info
    5   - music player now-playing display
    10  - sync / file transfer status
    20  - critical alerts, errors, low battery warnings

Calls are non-blocking and fire-and-forget over a Unix socket; if the
manager isn't running, calls fail silently (logged to stderr) so a
display issue never crashes the calling script.
"""

import json
import socket
import sys
import threading
import time

SOCKET_PATH = "/tmp/vfd_manager.sock"
HEARTBEAT_INTERVAL = 4.0  # seconds; should be well under the manager's CLAIM_TIMEOUT


def _send(msg, timeout=1.0):
    """Send one JSON message to the manager and return its response dict."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(SOCKET_PATH)
            sock.sendall(json.dumps(msg).encode("utf-8"))
            data = sock.recv(65536).decode("utf-8").strip()
            return json.loads(data) if data else {"ok": False, "error": "empty response"}
    except (FileNotFoundError, ConnectionRefusedError):
        print("[vfd_client] manager not running -- is vfd_manager.py started?",
              file=sys.stderr)
        return {"ok": False, "error": "manager not running"}
    except Exception as e:
        print(f"[vfd_client] error talking to manager: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}


class VFDClient:
    """
    A display claim for one script/process.

    Buffers draw commands locally and pushes the full command list to the
    manager on every call (simplest correct approach for a low-frequency
    display like this). A background thread sends periodic heartbeats so
    the manager knows this client is still alive.
    """

    def __init__(self, client_id, priority=0, auto_heartbeat=True):
        self.client_id = client_id
        self.priority = priority
        self._commands = []
        self._released = False
        self._hb_thread = None

        if auto_heartbeat:
            self._start_heartbeat()

    # ── Context manager support ──────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()

    # ── Drawing API — mirrors FutabaVFD's methods ────────────────────────────
    # Each call replaces the client's current command buffer with a single
    # command and immediately pushes it to the manager. For multi-step
    # sequences (e.g. write then progress bar), call batch() instead.

    def write_str(self, position, text):
        self._push([["write_str", [position, text]]])

    def write_char(self, position, char):
        self._push([["write_char", [position, char]]])

    def write_code(self, position, code):
        self._push([["write_code", [position, code]]])

    def write_symbol(self, position, name):
        self._push([["write_symbol", [position, name]]])

    def write_custom(self, slot, bitmap, position=None):
        args = [slot, bitmap] if position is None else [slot, bitmap, position]
        self._push([["write_custom", args]])

    def show_custom(self, position, slot):
        self._push([["show_custom", [position, slot]]])

    def clear(self, position=None):
        args = [] if position is None else [position]
        self._push([["clear", args]])

    def set_brightness(self, value):
        self._push([["set_brightness", [value]]])

    def on(self):
        self._push([["on", []]])

    def off(self):
        self._push([["off", []]])

    def scroll_h(self, text, delay=0.15, loops=1, pad=True):
        self._push([["scroll_h", [text, delay, loops, pad]]])

    def scroll_v(self, position, chars, delay=0.1, loops=1):
        self._push([["scroll_v", [position, chars, delay, loops]]])

    def blink(self, times=3, on_time=0.3, off_time=0.2):
        self._push([["blink", [times, on_time, off_time]]])

    def blink_position(self, position, code, times=3, on_time=0.3, off_time=0.2):
        self._push([["blink_position", [position, code, times, on_time, off_time]]])

    def progress_bar(self, fraction, prefix='', suffix=''):
        self._push([["progress_bar", [fraction, prefix, suffix]]])

    def progress_bar_animated(self, start, end, duration=2.0,
                               prefix='', suffix='', steps=40):
        self._push([["progress_bar_animated",
                     [start, end, duration, prefix, suffix, steps]]])

    def batch(self, commands):
        """
        Send several draw commands as one claim in one go.

        commands : list of [method_name, args_list]

        Example:
          client.batch([
              ["write_str", [0, "SYNCING "]],
              ["progress_bar", [0.5, '', '']],
          ])
        """
        self._push(commands)

    # ── Claim lifecycle ───────────────────────────────────────────────────────

    def _push(self, commands):
        if self._released:
            return
        self._commands = commands
        _send({
            "action": "claim",
            "client_id": self.client_id,
            "priority": self.priority,
            "commands": self._commands,
        })

    def release(self):
        """Give up the display claim. The manager will revert to the next
        highest-priority active claim, if any."""
        if self._released:
            return
        self._released = True
        _send({"action": "release", "client_id": self.client_id})

    def _start_heartbeat(self):
        def loop():
            while not self._released:
                _send({"action": "heartbeat", "client_id": self.client_id})
                time.sleep(HEARTBEAT_INTERVAL)
        self._hb_thread = threading.Thread(target=loop, daemon=True)
        self._hb_thread.start()


def get_status():
    """Return the manager's current state -- who owns the display, all
    active claims and their priorities. Useful for debugging."""
    return _send({"action": "status"})


# ── Command-line interface ────────────────────────────────────────────────────
# Lets shell scripts / udev triggers use this without writing Python.
#
# Examples:
#   python3 vfd_client.py claim sync_music 10 write_str 0 "SYNCING "
#   python3 vfd_client.py claim sync_music 10 progress_bar 0.5
#   python3 vfd_client.py release sync_music
#   python3 vfd_client.py status
if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]

    if cmd == "status":
        print(json.dumps(get_status(), indent=2))

    elif cmd == "release":
        client_id = args[1]
        result = _send({"action": "release", "client_id": client_id})
        print(json.dumps(result))

    elif cmd == "claim":
        # claim <client_id> <priority> <method> <arg1> <arg2> ...
        client_id = args[1]
        priority = float(args[2])
        method = args[3]
        raw_args = args[4:]

        # Best-effort type coercion: try float, then leave as string
        parsed_args = []
        for a in raw_args:
            try:
                parsed_args.append(float(a) if "." in a else int(a))
            except ValueError:
                parsed_args.append(a)

        result = _send({
            "action": "claim",
            "client_id": client_id,
            "priority": priority,
            "commands": [[method, parsed_args]],
        })
        print(json.dumps(result))

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
