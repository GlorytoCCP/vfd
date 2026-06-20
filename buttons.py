"""
buttons.py — GPIO Button Input Daemon
========================================
Runs continuously, listening for physical button presses via GPIO
interrupts. This is the ONLY process that should ever call
GPIO.add_event_detect() for these pins -- a pin can only have one
listener at a time, so all button handling lives here.

Two categories of buttons:

  NAVIGATION buttons (up/down/enter/back)
    Only forwarded to vfd_menu.py, and only when the menu is currently
    the foreground (highest priority) claim on the display. If
    something else (like sync_music) currently owns the display,
    these presses are ignored -- there's nothing to navigate right now.

  FIXED-FUNCTION buttons (play/pause, skip, etc.)
    Always call their target directly, regardless of what's on the
    display. (Stubbed out for now until the player script exists.)

Edit BUTTON_PINS below to match your wiring.

Run continuously in the background:
  python3 buttons.py
"""

import json
import socket
import time

import RPi.GPIO as GPIO

# ── Pin configuration — change to match your wiring (BCM numbering) ─────────
BUTTON_PINS = {
    "up":    5,
    "down":  6,
    "enter": 13,
    "back":  19,
    # Fixed-function buttons -- add pins as you wire them up
    "play_pause": 12,
    "skip_next":  16,
    "skip_prev":  20,
}

NAV_ACTIONS = {"up", "down", "enter", "back"}

BOUNCE_TIME_MS = 250  # debounce window

MENU_SOCKET    = "/tmp/vfd_menu.sock"
MANAGER_SOCKET = "/tmp/vfd_manager.sock"
PLAYER_SOCKET  = "/tmp/player.sock"


def _send_unix(path, payload, timeout=2.0):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall(json.dumps(payload).encode("utf-8"))
            data = sock.recv(4096).decode("utf-8").strip()
            return json.loads(data) if data else None
    except Exception as e:
        print(f"[buttons] error talking to {path}: {e}")
        return None


def menu_is_foreground():
    """Check with vfd_manager whether vfd_menu currently owns the display."""
    status = _send_unix(MANAGER_SOCKET, {"action": "status"})
    if not status or not status.get("ok"):
        return False
    return status.get("current_owner") == "vfd_menu"


def handle_nav_button(action):
    if menu_is_foreground():
        _send_unix(MENU_SOCKET, {"action": action})
    else:
        print(f"[buttons] '{action}' ignored -- menu is not foreground")


def handle_fixed_button(action):
    """
    Fixed-function buttons always do their job, regardless of what's on
    the display.
    """
    if action == "play_pause":
        _send_unix(PLAYER_SOCKET, {"action": "toggle"})
    elif action == "skip_next":
        _send_unix(PLAYER_SOCKET, {"action": "next"})
    elif action == "skip_prev":
        _send_unix(PLAYER_SOCKET, {"action": "prev"})


def make_callback(action):
    def callback(channel):
        if action in NAV_ACTIONS:
            handle_nav_button(action)
        else:
            handle_fixed_button(action)
    return callback


def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    for action, pin in BUTTON_PINS.items():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.add_event_detect(
            pin, GPIO.FALLING,
            callback=make_callback(action),
            bouncetime=BOUNCE_TIME_MS,
        )
        print(f"[buttons] listening on GPIO {pin} -> '{action}'")

    print("[buttons] ready")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        GPIO.cleanup()
        print("[buttons] shutting down")


if __name__ == "__main__":
    main()
