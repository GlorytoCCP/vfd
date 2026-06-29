import os
import shutil
import socket
import json
import time
from vfd_client import VFDClient

# Config
SOURCE_PATH = "/media/pendrive"
DEST_PATH = "/home/mateusz/music"
FILE_TYPES = (".mp3", ".wav", ".flac", ".aac", ".m4a")

PRIORITY = 10

FINAL_HOLD_SECONDS = 3.0


def refresh_menu():
    """Tell vfd_menu.py to re-scan its current folder, so newly synced
    files show up immediately instead of requiring a reboot."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect("/tmp/vfd_menu.sock")
            sock.sendall(json.dumps({"action": "refresh"}).encode("utf-8"))
            sock.recv(4096)
    except Exception as e:
        print(f"[sync_music] could not refresh menu: {e}")


def sync_music(vfd):
    os.makedirs(DEST_PATH, exist_ok=True)

    vfd.write_str(0, "LOOKING ")

    files = []
    for root, dirs, filenames in os.walk(SOURCE_PATH):
        for file in filenames:
            if file.lower().endswith(FILE_TYPES) and not file.startswith("._"):
                full_source = os.path.join(root, file)
                rel_path = os.path.relpath(full_source, SOURCE_PATH)
                files.append((full_source, rel_path))

    if not files:
        vfd.write_str(0, "ERROR404")
        time.sleep(2)
        return

    vfd.write_str(0, f"FOUND{len(files):>3}")
    time.sleep(1.5)

    copied = 0
    copied_names = []
    skipped = 0
    total = len(files)

    # --- This loop ONLY copies files and updates the progress bar.
    # The "what happened" message is shown ONCE, after the loop ends.
    for full_source, rel_path in files:
        dest_file = os.path.join(DEST_PATH, rel_path)

        if os.path.exists(dest_file):
            skipped += 1
        else:
            os.makedirs(os.path.dirname(dest_file), exist_ok=True)
            shutil.copy2(full_source, dest_file)
            copied += 1
            copied_names.append(os.path.basename(rel_path))

        vfd.progress_bar((copied + skipped) / total, wait=True, hold=0.15)

    # --- Runs exactly once, after every file has been processed.
    if copied == 0:
        vfd.scroll_h("Sorry, no files were copied", delay=0.1)
        time.sleep(2)
    else:
        vfd.scroll_h(f"Copied total {copied:>3}")
        for name in copied_names:
            vfd.scroll_h(name)
        vfd.scroll_h(f"Skipped{skipped:>3}")

    refresh_menu()  # so newly copied files show up in the menu immediately
    time.sleep(FINAL_HOLD_SECONDS)


if __name__ == "__main__":
    with VFDClient("sync_music", priority=PRIORITY) as vfd:
        sync_music(vfd)
