import os
import shutil
import time
from vfd_client import VFDClient

# Config
SOURCE_PATH = "/media/pendrive"
DEST_PATH = "/home/mateusz/music"
FILE_TYPES = (".mp3", ".wav", ".flac", ".aac", ".m4a")

# High priority -- sync should take over the display from the music player
# (or anything else) while it's running, then hand it back when done.
PRIORITY = 10

# How long to hold the final summary on screen before releasing the
# display back to the menu/idle -- guards against the manager's display
# thread still catching up on a backlog of rapid progress bar updates.
FINAL_HOLD_SECONDS = 3.0


def sync_music(vfd):
    os.makedirs(DEST_PATH, exist_ok=True)

    vfd.write_str(0, "SCANNING")

    # Walk the source, keeping each file's path RELATIVE to SOURCE_PATH so
    # we can recreate the same folder structure under DEST_PATH. This means
    # albums/folders on the drive show up as their own browsable folders in
    # the menu, instead of being flattened into one big list.
    files = []  # list of (full_source_path, relative_path)
    for root, dirs, filenames in os.walk(SOURCE_PATH):
        for file in filenames:
            if file.lower().endswith(FILE_TYPES) and not file.startswith("._"):
                full_source = os.path.join(root, file)
                rel_path = os.path.relpath(full_source, SOURCE_PATH)
                files.append((full_source, rel_path))

    if not files:
        vfd.write_str(0, "NO FILES")
        time.sleep(2)
        return

    vfd.write_str(0, f"FOUND{len(files):>3}")
    time.sleep(1.5)

    copied = 0
    skipped = 0
    total = len(files)

    for full_source, rel_path in files:
        dest_file = os.path.join(DEST_PATH, rel_path)

        if os.path.exists(dest_file):
            skipped += 1
        else:
            # Recreate the same sub-folder structure under DEST_PATH
            os.makedirs(os.path.dirname(dest_file), exist_ok=True)
            shutil.copy2(full_source, dest_file)
            copied += 1

        # Show progress as a bar representing files processed so far.
        # wait=True ensures this frame actually renders before the next
        # file is processed, instead of risking it being silently dropped
        # by a rapid-fire claim from the next iteration.
        vfd.progress_bar((copied + skipped) / total, wait=True, hold=0.15)

    vfd.write_str(0, f"OK{copied:>3}/{skipped:>3}")
    time.sleep(FINAL_HOLD_SECONDS)


if __name__ == "__main__":
    # Claim the display at high priority for the duration of the sync.
    # release() (called automatically on exit) hands the display back to
    # whatever was showing before -- e.g. the music player resumes.
    with VFDClient("sync_music", priority=PRIORITY) as vfd:
        sync_music(vfd)
