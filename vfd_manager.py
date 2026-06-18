"""
vfd_manager.py — Display Priority Manager Daemon
=================================================
Runs continuously in the background and is the ONLY process that ever
touches the FutabaVFD object directly. Other scripts never import
futaba_vfd themselves — they send requests to this manager instead,
through the vfd_client module.

How it works:
  - Each client "claims" the display with a priority number and a list
    of draw commands.
  - The manager always shows whichever active claim has the HIGHEST
    priority.
  - A lower-priority client's commands are simply not drawn while a
    higher-priority client is active — but the lower-priority script
    itself keeps running normally in the background (it just doesn't
    reach the screen).
  - When a high-priority client releases its claim, the manager
    automatically reverts to the next-highest active claim (e.g. music
    player resumes display after a sync finishes).

Run this once at boot (e.g. via systemd or a @reboot cron entry):
  python3 vfd_manager.py

Communication is over a Unix domain socket at /tmp/vfd_manager.sock
using simple line-delimited JSON messages.
"""

import json
import os
import socket
import socketserver
import threading
import time

from futaba_vfd import FutabaVFD, SYMBOLS  # noqa: F401  (SYMBOLS re-exported for convenience)

SOCKET_PATH = "/tmp/vfd_manager.sock"

# ── Pin configuration — change to match your wiring ──────────────────────────
VFD_PINS = dict(din=21, clk=20, cs=16, rst=26, en=19)

# How often (seconds) the manager re-checks whether anything has timed out
# or needs idle handling.
TICK_INTERVAL = 0.2

# If a client hasn't sent a "heartbeat" refresh within this many seconds,
# its claim is considered stale and automatically released. Prevents a
# crashed script from permanently hogging the display.
CLAIM_TIMEOUT = 10.0


class Claim:
    """Represents one client's active hold on the display."""
    def __init__(self, client_id, priority, commands):
        self.client_id = client_id
        self.priority = priority
        self.commands = commands       # list of [method, args] to replay
        self.last_seen = time.time()


class VFDManager:
    def __init__(self):
        self.vfd = FutabaVFD(**VFD_PINS)
        self.claims = {}                # client_id -> Claim
        self.lock = threading.Lock()
        self.current_owner = None       # client_id currently being displayed
        self._running = True

    # ── Claim handling ───────────────────────────────────────────────────────

    def handle_message(self, msg):
        """Process one incoming request and return a response dict."""
        action = msg.get("action")

        if action == "claim":
            return self._claim(msg)
        elif action == "release":
            return self._release(msg)
        elif action == "heartbeat":
            return self._heartbeat(msg)
        elif action == "status":
            return self._status()
        else:
            return {"ok": False, "error": f"unknown action '{action}'"}

    def _claim(self, msg):
        client_id = msg["client_id"]
        priority = msg["priority"]
        commands = msg["commands"]  # list of [method_name, args_list]

        with self.lock:
            self.claims[client_id] = Claim(client_id, priority, commands)
            self._refresh_display()

        return {"ok": True}

    def _release(self, msg):
        client_id = msg["client_id"]
        with self.lock:
            self.claims.pop(client_id, None)
            self._refresh_display()
        return {"ok": True}

    def _heartbeat(self, msg):
        client_id = msg["client_id"]
        with self.lock:
            if client_id in self.claims:
                self.claims[client_id].last_seen = time.time()
        return {"ok": True}

    def _status(self):
        with self.lock:
            return {
                "ok": True,
                "current_owner": self.current_owner,
                "claims": {
                    cid: {"priority": c.priority}
                    for cid, c in self.claims.items()
                },
            }

    # ── Display refresh logic ────────────────────────────────────────────────

    def _refresh_display(self):
        """Show whichever active claim has the highest priority. Caller must
        hold self.lock."""
        if not self.claims:
            if self.current_owner is not None:
                self.vfd.clear()
                self.current_owner = None
            return

        top_client_id, top_claim = max(
            self.claims.items(), key=lambda kv: kv[1].priority
        )

        if top_client_id != self.current_owner:
            self.current_owner = top_client_id
            self.vfd.clear()

        self._replay(top_claim.commands)

    def _replay(self, commands):
        """Execute a list of [method_name, args] against the real VFD."""
        for method_name, args in commands:
            method = getattr(self.vfd, method_name, None)
            if method is None:
                continue
            try:
                method(*args)
            except Exception as e:
                print(f"[vfd_manager] error replaying {method_name}{args}: {e}")

    # ── Background housekeeping ──────────────────────────────────────────────

    def _watchdog_loop(self):
        """Periodically clear out stale claims from crashed/dead clients."""
        while self._running:
            time.sleep(TICK_INTERVAL)
            now = time.time()
            with self.lock:
                stale = [
                    cid for cid, c in self.claims.items()
                    if now - c.last_seen > CLAIM_TIMEOUT
                ]
                for cid in stale:
                    print(f"[vfd_manager] claim '{cid}' timed out, releasing")
                    del self.claims[cid]
                if stale:
                    self._refresh_display()

    def start_watchdog(self):
        t = threading.Thread(target=self._watchdog_loop, daemon=True)
        t.start()

    def shutdown(self):
        self._running = False
        self.vfd.close()


# ── Socket server plumbing ────────────────────────────────────────────────────

manager = None  # set in main()


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            data = self.request.recv(65536).decode("utf-8").strip()
        except (ConnectionResetError, OSError):
            return
        if not data:
            return
        try:
            msg = json.loads(data)
            response = manager.handle_message(msg)
        except Exception as e:
            response = {"ok": False, "error": str(e)}
        try:
            self.request.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client already gave up / disconnected -- safe to ignore.
            pass


class UnixSocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global manager

    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)

    manager = VFDManager()
    manager.start_watchdog()

    server = UnixSocketServer(SOCKET_PATH, Handler)
    print(f"[vfd_manager] listening on {SOCKET_PATH}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("[vfd_manager] shutting down")
        server.shutdown()
        manager.shutdown()
        if os.path.exists(SOCKET_PATH):
            os.remove(SOCKET_PATH)


if __name__ == "__main__":
    main()
