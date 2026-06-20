"""
player.py — Music Player Daemon
==================================
A standalone daemon that owns one mpv instance and exposes playback
control over a Unix socket. buttons.py calls into this directly for
play/pause/skip (fixed-function buttons -- always work, regardless of
what's currently on the display).

Shows now-playing info on the VFD at a priority between idle and
sync_music (tune PRIORITY below to fit your overall scheme).

Run continuously in the background:
  python3 player.py

Manual testing from another terminal:
  python3 player.py --send play /home/mateusz/music/song.mp3
  python3 player.py --send pause
  python3 player.py --send resume
  python3 player.py --send toggle
  python3 player.py --send stop
  python3 player.py --send next
  python3 player.py --send prev
"""

import json
import os
import socket
import socketserver
import sys
import threading
import time

import mpv
from mutagen import File as MutagenFile

from vfd_client import VFDClient

SOCKET_PATH = "/tmp/player.sock"
PRIORITY = 6   # between idle (1) and sync_music (10) -- tune as you like

# ── Audio output device ───────────────────────────────────────────────────────
# Change this single line to switch where audio comes out.
#
#   Native 3.5mm jack / HDMI (default):
#       AUDIO_DEVICE = "alsa/default"
#
#   Force the 3.5mm jack specifically (after running:
#   `sudo raspi-config` -> Advanced Options -> Audio -> Force 3.5mm jack):
#       AUDIO_DEVICE = "alsa/default"   (raspi-config handles the routing)
#
#   Bluetooth speaker/headphones, once paired & connected (find the MAC
#   with `bluetoothctl devices`):
#       AUDIO_DEVICE = "alsa/bluealsa:DEV=XX:XX:XX:XX:XX:XX"
#   or, if using PulseAudio/PipeWire instead of plain ALSA:
#       AUDIO_DEVICE = None   # leave unset, and instead set AO_DRIVER = "pulse" below
#
#   I2S DAC / HAT (e.g. HiFiBerry), after enabling its overlay in
#   /boot/config.txt -- find the card name with `aplay -L`:
#       AUDIO_DEVICE = "alsa/hw:CARD=sndrpihifiberry,DEV=0"
#
AUDIO_DEVICE = "alsa/default"

# Only needed if you want to switch the whole audio output driver (rare --
# most setups just need AUDIO_DEVICE above). Leave as None for the default.
# Set to "pulse" for PulseAudio/PipeWire-based Bluetooth setups.
AO_DRIVER = None

MUSIC_ROOT = "/home/mateusz/music"


def get_metadata(filepath):
    """Best-effort tag read. Falls back to the filename if tags are missing."""
    title = os.path.splitext(os.path.basename(filepath))[0]
    artist = ""
    try:
        tags = MutagenFile(filepath, easy=True)
        if tags:
            if tags.get("title"):
                title = tags["title"][0]
            if tags.get("artist"):
                artist = tags["artist"][0]
    except Exception as e:
        print(f"[player] tag read error for {filepath}: {e}")
    return title, artist


class Player:
    def __init__(self):
        mpv_kwargs = dict(video=False, ytdl=False)
        if AUDIO_DEVICE:
            mpv_kwargs["audio_device"] = AUDIO_DEVICE
        if AO_DRIVER:
            mpv_kwargs["ao"] = AO_DRIVER

        self.mp = mpv.MPV(**mpv_kwargs)
        self.vfd = VFDClient("player", priority=PRIORITY)

        self.current_path = None
        self.playlist = []      # full list of files in the current folder
        self.playlist_index = -1
        self.lock = threading.Lock()
        self._manual_stop = False  # set just before mp.stop() so the
                                    # end-file callback knows not to auto-advance
        self._display_thread = None

        # React to natural end-of-track to auto-advance
        @self.mp.event_callback('end-file')
        def _on_end(event):
            if self._manual_stop:
                self._manual_stop = False  # consume the flag
                return
            self._auto_advance()

    # ── Core controls ─────────────────────────────────────────────────────────

    def play(self, filepath, playlist=None, index=None):
        """
        Start playing filepath. Optionally provide the sibling playlist
        (list of full paths) and this track's index within it, so
        next()/prev() can navigate without the menu's involvement.
        """
        with self.lock:
            self.current_path = filepath
            if playlist is not None:
                self.playlist = playlist
                self.playlist_index = index if index is not None else 0
            else:
                # No playlist given -- build one from the file's own folder
                folder = os.path.dirname(filepath)
                self.playlist = self._scan_folder(folder)
                try:
                    self.playlist_index = self.playlist.index(filepath)
                except ValueError:
                    self.playlist_index = 0

        self.mp.play(filepath)
        self._show_now_playing()

    def pause(self):
        self.mp.pause = True
        self._show_now_playing()

    def resume(self):
        self.mp.pause = False
        self._show_now_playing()

    def toggle(self):
        self.mp.pause = not self.mp.pause
        self._show_now_playing()

    def stop(self):
        with self.lock:
            self.current_path = None
        self._manual_stop = True
        try:
            self.mp.stop()
        except Exception:
            pass
        self.vfd.release()

    def next(self):
        with self.lock:
            if not self.playlist:
                return
            new_index = self.playlist_index + 1
            if new_index >= len(self.playlist):
                return  # no wrap -- stop at the end, consistent with menu behaviour
            self.playlist_index = new_index
            filepath = self.playlist[new_index]
        self._manual_stop = True  # the upcoming play() will end the current file
        self.play(filepath, playlist=self.playlist, index=new_index)

    def prev(self):
        with self.lock:
            if not self.playlist:
                return
            new_index = self.playlist_index - 1
            if new_index < 0:
                return
            self.playlist_index = new_index
            filepath = self.playlist[new_index]
        self._manual_stop = True
        self.play(filepath, playlist=self.playlist, index=new_index)

    def _auto_advance(self):
        """Called when a track finishes naturally -- move to the next one."""
        time.sleep(0.2)  # let mpv settle before issuing the next play
        self.next()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _scan_folder(self, folder):
        types = (".mp3", ".wav", ".flac", ".aac", ".m4a")
        try:
            names = sorted(f for f in os.listdir(folder)
                           if f.lower().endswith(types) and not f.startswith("."))
        except FileNotFoundError:
            return []
        return [os.path.join(folder, n) for n in names]

    def _show_now_playing(self):
        if not self.current_path:
            return
        title, artist = get_metadata(self.current_path)
        status = "||" if self.mp.pause else ">"
        text = f"{status} {artist} - {title}  " if artist else f"{status} {title}  "
        self.vfd.scroll_h(text, delay=0.15, loops=1, wait=False)
        self._ensure_display_loop()

    def _ensure_display_loop(self):
        """Start a background thread that keeps re-showing the now-playing
        scroll repeatedly, so the display doesn't go blank after one pass
        (scroll_h is a one-shot animation -- it won't repeat on its own)."""
        if self._display_thread is not None and self._display_thread.is_alive():
            return

        def loop():
            while self.current_path is not None:
                title, artist = get_metadata(self.current_path)
                status = "||" if self.mp.pause else ">"
                text = (f"{status} {artist} - {title}  " if artist
                       else f"{status} {title}  ")
                # scroll_h with wait=True blocks for the right duration,
                # which is exactly what we want for a steady repeating loop
                self.vfd.scroll_h(text, delay=0.15, loops=1, wait=True)
                time.sleep(0.5)  # brief pause between repeats

        self._display_thread = threading.Thread(target=loop, daemon=True)
        self._display_thread.start()


# ── Socket server ──────────────────────────────────────────────────────────────

player = None


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            data = self.request.recv(8192).decode("utf-8").strip()
        except (ConnectionResetError, OSError):
            return
        if not data:
            return

        try:
            msg = json.loads(data)
        except Exception:
            msg = {"action": data}

        action = msg.get("action")
        response = {"ok": True}

        try:
            if action == "play":
                player.play(msg["path"], msg.get("playlist"), msg.get("index"))
            elif action == "pause":
                player.pause()
            elif action == "resume":
                player.resume()
            elif action == "toggle":
                player.toggle()
            elif action == "stop":
                player.stop()
            elif action == "next":
                player.next()
            elif action == "prev":
                player.prev()
            elif action == "status":
                response["current_path"] = player.current_path
                response["paused"] = player.mp.pause if player.current_path else None
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
    global player

    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)

    player = Player()

    server = UnixSocketServer(SOCKET_PATH, Handler)
    print(f"[player] listening on {SOCKET_PATH}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("[player] shutting down")
        server.shutdown()
        player.stop()
        if os.path.exists(SOCKET_PATH):
            os.remove(SOCKET_PATH)


# ── Manual CLI testing ───────────────────────────────────────────────────────────

def send_action(action, path=None):
    payload = {"action": action}
    if path:
        payload["path"] = path
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(3.0)
            sock.connect(SOCKET_PATH)
            sock.sendall(json.dumps(payload).encode("utf-8"))
            data = sock.recv(8192).decode("utf-8").strip()
            print(data)
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--send":
        action = sys.argv[2]
        path = sys.argv[3] if len(sys.argv) > 3 else None
        send_action(action, path)
    else:
        main()
