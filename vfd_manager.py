"""
vfd_manager.py — Display Priority Manager Daemon
=================================================
Runs continuously in the background and is the ONLY process that ever
touches the FutabaVFD object directly. Other scripts never import
futaba_vfd themselves — they send requests to this manager instead,
through the vfd_client module.

Run once at boot:
  python3 vfd_manager.py

Communication is over a Unix domain socket at /tmp/vfd_manager.sock
using simple line-delimited JSON messages.
"""

import json
import os
import queue
import socket
import socketserver
import threading
import time

from futaba_vfd import FutabaVFD, SYMBOLS  # noqa

SOCKET_PATH   = "/tmp/vfd_manager.sock"
VFD_PINS      = dict(din=21, clk=20, cs=16, rst=26, en=19)
CLAIM_TIMEOUT = 10.0   # seconds before a silent client's claim is dropped


# ── Claim ─────────────────────────────────────────────────────────────────────

class Claim:
    def __init__(self, client_id, priority, commands):
        self.client_id  = client_id
        self.priority   = priority
        self.commands   = commands
        self.last_seen  = time.time()


# ── Display thread ────────────────────────────────────────────────────────────
# The ONLY thread that ever calls FutabaVFD methods.
# Everything else communicates with it through self.display_queue.

class VFDManager:
    def __init__(self):
        self.vfd           = FutabaVFD(**VFD_PINS)
        self.claims        = {}          # client_id -> Claim
        self.claims_lock   = threading.Lock()
        self.current_owner = None
        self.display_queue = queue.Queue()
        self._running      = True

        # Start the display thread
        t = threading.Thread(target=self._display_loop, daemon=True)
        t.start()

        # Start the watchdog thread
        w = threading.Thread(target=self._watchdog_loop, daemon=True)
        w.start()

    # ── Public API (called from socket handler threads) ───────────────────────

    def handle_message(self, msg):
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

    # ── Claim management (socket handler threads) ─────────────────────────────

    # Commands that are animations — run once and never replayed on refresh.
    # Replaying these would restart the animation from the beginning.
    _ONESHOT_COMMANDS = frozenset({
        "scroll_h", "scroll_v", "blink", "blink_position",
        "progress_bar_animated",
    })

    def _claim(self, msg):
        client_id = msg["client_id"]
        priority  = msg["priority"]
        commands  = msg["commands"]
        is_oneshot = any(cmd[0] in self._ONESHOT_COMMANDS for cmd in commands)
        with self.claims_lock:
            claim = Claim(client_id, priority, commands)
            claim.is_oneshot = is_oneshot
            self.claims[client_id] = claim
            self._enqueue_refresh(force_replay=True)
        return {"ok": True}

    def _release(self, msg):
        client_id = msg["client_id"]
        with self.claims_lock:
            self.claims.pop(client_id, None)
            self._enqueue_refresh(force_replay=True)
        return {"ok": True}

    def _heartbeat(self, msg):
        client_id = msg["client_id"]
        with self.claims_lock:
            if client_id in self.claims:
                self.claims[client_id].last_seen = time.time()
        return {"ok": True}

    def _status(self):
        with self.claims_lock:
            return {
                "ok": True,
                "current_owner": self.current_owner,
                "claims": {
                    cid: {"priority": c.priority}
                    for cid, c in self.claims.items()
                },
            }

    def _enqueue_refresh(self, force_replay=False):
        """Ask the display thread to re-evaluate who should be shown.
        Caller must hold claims_lock.
        force_replay=True: always replay commands (used when claim changes).
        force_replay=False: only replay non-oneshot commands (used on owner-change only)."""
        if not self.claims:
            top  = None
            cmds = []
            oneshot = False
        else:
            _, top_claim = max(self.claims.items(),
                               key=lambda kv: kv[1].priority)
            top     = top_claim.client_id
            cmds    = top_claim.commands
            oneshot = getattr(top_claim, 'is_oneshot', False)

        # Skip replay of one-shot animations unless this is a forced refresh
        # (i.e. a new command just arrived). This prevents scroll_h etc from
        # restarting when an unrelated lower-priority claim refreshes.
        if oneshot and not force_replay:
            return

        # Drop any pending refresh already in the queue
        while not self.display_queue.empty():
            try:
                self.display_queue.get_nowait()
            except queue.Empty:
                break

        self.display_queue.put((top, cmds))

    # ── Display thread ────────────────────────────────────────────────────────

    def _display_loop(self):
        """Sole consumer of display_queue. Only this thread touches self.vfd."""
        while self._running:
            try:
                owner, commands = self.display_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if owner != self.current_owner:
                self.current_owner = owner
                try:
                    self.vfd.clear()
                except Exception as e:
                    print(f"[vfd_manager] clear error: {e}")

            if commands:
                for method_name, args in commands:
                    method = getattr(self.vfd, method_name, None)
                    if method is None:
                        continue
                    try:
                        method(*args)
                    except Exception as e:
                        print(f"[vfd_manager] replay error {method_name}{args}: {e}")

    # ── Watchdog thread ───────────────────────────────────────────────────────

    def _watchdog_loop(self):
        while self._running:
            time.sleep(1.0)
            now = time.time()
            with self.claims_lock:
                stale = [cid for cid, c in self.claims.items()
                         if now - c.last_seen > CLAIM_TIMEOUT]
                if stale:
                    for cid in stale:
                        print(f"[vfd_manager] claim '{cid}' timed out")
                        del self.claims[cid]
                    self._enqueue_refresh(force_replay=True)

    def shutdown(self):
        self._running = False
        self.vfd.close()


# ── Socket server ─────────────────────────────────────────────────────────────

manager = None


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            data = self.request.recv(65536).decode("utf-8").strip()
        except (ConnectionResetError, OSError):
            return
        if not data:
            return
        try:
            msg      = json.loads(data)
            response = manager.handle_message(msg)
        except Exception as e:
            response = {"ok": False, "error": str(e)}
        try:
            self.request.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client already disconnected, safe to ignore


class UnixSocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    allow_reuse_address = True
    daemon_threads      = True


def main():
    global manager

    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)

    manager = VFDManager()

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
