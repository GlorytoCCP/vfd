"""
vfd_menu.py — Music Browser Menu Daemon
=========================================
A standalone daemon that maintains the current folder/file listing and
selection state, and draws the menu on the VFD via vfd_client whenever
it's the active (highest priority) display owner.

It does NOT read GPIO buttons directly. Instead it listens on its own
Unix socket for simple action commands ("up", "down", "enter", "back"),
sent by buttons.py (or anything else — e.g. you can test it by hand
from the command line).

Run continuously in the background:
  python3 vfd_menu.py

Test manually from another terminal:
  python3 -c "from menu_client import send_action; send_action('down')"

Or via the command line interface built into this file:
  python3 vfd_menu.py --send down
  python3 vfd_menu.py --send enter
  python3 vfd_menu.py --send back
"""

import json
import os
import socket
import socketserver
import sys
import threading
import time

from vfd_client import VFDClient

SOCKET_PATH = "/tmp/vfd_menu.sock"
ROOT_PATH = "/home/mateusz/music"
PRIORITY = 5  # tune later, per your plan to make this the default interface

AUDIO_TYPES = (".mp3", ".wav", ".flac", ".aac", ".m4a")

SCROLL_DELAY = 0.15   # seconds per scroll step for long filenames
SCROLL_PAUSE = 0.6    # pause before a long filename starts scrolling


def list_dir(path):
    """
    Return a sorted list of entries in path as (name, is_dir) tuples.
    Folders are listed first, then audio files. Hidden/system files and
    non-audio files are skipped.
    """
    entries = []
    try:
        for name in sorted(os.listdir(path)):
            if name.startswith("."):
                continue
            full = os.path.join(path, name)
            if os.path.isdir(full):
                entries.append((name, True))
            elif name.lower().endswith(AUDIO_TYPES):
                entries.append((name, False))
    except FileNotFoundError:
        pass

    # Folders first, then files, each alphabetically (already sorted above,
    # this just re-groups since os.listdir order isn't type-aware)
    dirs  = [e for e in entries if e[1]]
    files = [e for e in entries if not e[1]]
    return dirs + files


class MenuState:
    def __init__(self):
        self.current_path = ROOT_PATH
        self.entries = list_dir(self.current_path)
        self.selected = 0

        # Scrolling state for the currently selected (possibly long) entry
        self._scroll_thread = None
        self._scroll_stop = threading.Event()

        self.lock = threading.Lock()

    # ── Navigation actions ────────────────────────────────────────────────────

    def up(self, vfd):
        with self.lock:
            if self.selected > 0:
                self.selected -= 1
        self._redraw(vfd)

    def down(self, vfd):
        with self.lock:
            if self.selected < len(self.entries) - 1:
                self.selected += 1
        self._redraw(vfd)

    def enter(self, vfd):
        with self.lock:
            if not self.entries:
                return
            name, is_dir = self.entries[self.selected]
            full = os.path.join(self.current_path, name)

            if is_dir:
                self.current_path = full
                self.entries = list_dir(full)
                self.selected = 0
            else:
                # It's a file -- trigger playback.
                # (Hook point: send this path to your music player script.)
                self._play(full)
                return  # don't redraw the menu over playback feedback
        self._redraw(vfd)

    def back(self, vfd):
        with self.lock:
            if self.current_path == ROOT_PATH:
                return  # already at the top, nowhere to go back to
            parent = os.path.dirname(self.current_path)
            old_folder_name = os.path.basename(self.current_path)
            self.current_path = parent
            self.entries = list_dir(parent)
            # Try to reselect the folder we just came from, for a nicer feel
            try:
                self.selected = [e[0] for e in self.entries].index(old_folder_name)
            except ValueError:
                self.selected = 0
        self._redraw(vfd)

    # ── Playback hook (stub for now) ─────────────────────────────────────────

    def _play(self, filepath):
        """Send the selected file to the player daemon, with the sibling
        playlist so next()/prev() work without needing the menu."""
        folder = os.path.dirname(filepath)
        siblings = [os.path.join(folder, n) for n in
                   [e[0] for e in self.entries if not e[1]]]
        try:
            index = siblings.index(filepath)
        except ValueError:
            index = 0

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(3.0)
                sock.connect("/tmp/player.sock")
                sock.sendall(json.dumps({
                    "action": "play",
                    "path": filepath,
                    "playlist": siblings,
                    "index": index,
                }).encode("utf-8"))
                sock.recv(4096)
        except Exception as e:
            print(f"[vfd_menu] could not reach player daemon: {e}")

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _redraw(self, vfd):
        """Stop any in-progress scroll and redraw the current selection."""
        self._stop_scroll()

        with self.lock:
            if not self.entries:
                vfd.write_str(0, "EMPTY   ")
                return
            name, is_dir = self.entries[self.selected]
            label = name
            if is_dir:
                # Strip extension display concerns -- folders just show as-is
                label = name

        if len(label) <= 8:
            vfd.write_str(0, label.ljust(8))
        else:
            self._start_scroll(vfd, label)

    def _start_scroll(self, vfd, text):
        self._scroll_stop.clear()

        def loop():
            time.sleep(SCROLL_PAUSE)
            if self._scroll_stop.is_set():
                return
            padded = " " * 8 + text + " " * 8
            while not self._scroll_stop.is_set():
                for start in range(len(padded) - 8 + 1):
                    if self._scroll_stop.is_set():
                        return
                    window = padded[start:start + 8]
                    vfd.write_str(0, window)
                    time.sleep(SCROLL_DELAY)
                    if self._scroll_stop.is_set():
                        return
                time.sleep(SCROLL_PAUSE)
                if self._scroll_stop.is_set():
                    return

        self._scroll_thread = threading.Thread(target=loop, daemon=True)
        self._scroll_thread.start()

    def _stop_scroll(self):
        """Signal the scroll thread to stop and wait for it to fully exit
        before the caller draws anything new -- prevents a stale scroll
        frame from racing with the next redraw."""
        self._scroll_stop.set()
        if self._scroll_thread is not None:
            self._scroll_thread.join(timeout=1.0)
        self._scroll_thread = None


# ── Socket server for receiving actions ───────────────────────────────────────

state = None
vfd_client = None


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            data = self.request.recv(4096).decode("utf-8").strip()
        except (ConnectionResetError, OSError):
            return
        if not data:
            return

        try:
            msg = json.loads(data)
            action = msg.get("action")
        except Exception:
            action = data  # allow plain-text "up"/"down"/etc too

        response = {"ok": True}
        try:
            if action == "up":
                state.up(vfd_client)
            elif action == "down":
                state.down(vfd_client)
            elif action == "enter":
                state.enter(vfd_client)
            elif action == "back":
                state.back(vfd_client)
            elif action == "ping":
                pass
            else:
                response = {"ok": False, "error": f"unknown action '{action}'"}
        except Exception as e:
            response = {"ok": False, "error": str(e)}

        try:
            self.request.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class UnixSocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global state, vfd_client

    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)

    state = MenuState()
    vfd_client = VFDClient("vfd_menu", priority=PRIORITY)

    # Draw the initial menu state immediately on startup
    state._redraw(vfd_client)

    server = UnixSocketServer(SOCKET_PATH, Handler)
    print(f"[vfd_menu] listening on {SOCKET_PATH}, browsing {ROOT_PATH}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("[vfd_menu] shutting down")
        server.shutdown()
        vfd_client.release()
        if os.path.exists(SOCKET_PATH):
            os.remove(SOCKET_PATH)


# ── Simple CLI for manual testing ─────────────────────────────────────────────

def send_action(action):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(SOCKET_PATH)
            sock.sendall(json.dumps({"action": action}).encode("utf-8"))
            data = sock.recv(4096).decode("utf-8").strip()
            print(data)
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--send":
        send_action(sys.argv[2])
    else:
        main()
