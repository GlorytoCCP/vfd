"""
Futaba 8-MD-06INKM VFD Display Driver for Raspberry Pi
=======================================================
Soft (bit-banged) SPI — use any GPIO pins you like.

Wiring — connect to any free GPIO pins and pass them in:
  VFD DIN  -> any GPIO (data)
  VFD CLK  -> any GPIO (clock)
  VFD CS   -> any GPIO (chip select, active low)
  VFD RST  -> any GPIO (reset, active low)
  VFD EN   -> any GPIO (enable) or None if not present
  VFD VCC  -> 5V
  VFD GND  -> GND

Install dependency:
  sudo apt install python3-rpi.gpio
  or: pip install RPi.GPIO

Basic usage:
  from futaba_vfd import FutabaVFD
  vfd = FutabaVFD(din=17, clk=27, cs=22, rst=23, en=24)
  vfd.write_str(0, "HELLO   ")
  vfd.scroll_h("HELLO WORLD", delay=0.15)
  vfd.scroll_v(0, "AB", delay=0.1)
  vfd.blink(times=3)
  vfd.progress_bar(0.75)
  vfd.set_brightness(200)
  vfd.clear()
  vfd.close()
"""

import time
import RPi.GPIO as GPIO

# ── Command bytes (Futaba 8-MD-06INKM datasheet) ──────────────────────────────
_DCRAM_DATA_WRITE   = 0x20  # write character to display RAM at position
_DGRAM_DATA_CLEAR   = 0x10  # blank a single position
_CGRAM_DATA_WRITE   = 0x40  # write custom 5×7 bitmap to character RAM
_SET_DISPLAY_TIMING = 0xE0  # set number of active digits
_SET_DIMMING_DATA   = 0xE4  # set brightness (0–255)
_SET_DISPLAY_ON     = 0xE8  # wake display
_SET_DISPLAY_OFF    = 0xEA  # all segments off
_SET_STAND_BY_ON    = 0xEC  # standby (low power)
_SET_STAND_BY_OFF   = 0xED  # leave standby

# ── Progress-bar characters stored in CGRAM slots 0–4 ────────────────────────
# Each character is 5 columns wide (bytes), one byte per column, LSB = top row.
# We create 5 fill levels: 0/5 … 5/5 columns filled.
_BAR_BITMAPS = [
    [0x00, 0x00, 0x00, 0x00, 0x00],  # slot 0 — empty cell
    [0x7F, 0x00, 0x00, 0x00, 0x00],  # slot 1 — 1/5 filled
    [0x7F, 0x7F, 0x00, 0x00, 0x00],  # slot 2 — 2/5 filled
    [0x7F, 0x7F, 0x7F, 0x00, 0x00],  # slot 3 — 3/5 filled
    [0x7F, 0x7F, 0x7F, 0x7F, 0x00],  # slot 4 — 4/5 filled
    # slot 5 would be full, but we just use ASCII 0xFF for a fully filled cell
]
_BAR_FULL_CHAR = 0xFF  # solid block (built-in character)


class FutabaVFD:
    def __init__(self, din, clk, cs, rst, en=None,
                 digits=8, brightness=255, spi_delay=0.000001):
        """
        Initialise the display using software (bit-banged) SPI.

        din        : BCM GPIO for data (MOSI)
        clk        : BCM GPIO for clock
        cs         : BCM GPIO for chip select (active low)
        rst        : BCM GPIO for reset (active low)
        en         : BCM GPIO for enable, or None if not present
        digits     : number of character positions (default 8)
        brightness : initial brightness 0–255
        spi_delay  : half-period of soft SPI clock in seconds (default 1 µs)
        """
        self.digits     = digits
        self.brightness = brightness
        self._delay     = spi_delay
        self._din       = din
        self._clk       = clk
        self._cs        = cs
        self._rst       = rst
        self._en        = en

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in (din, clk, cs, rst):
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
        if en is not None:
            GPIO.setup(en, GPIO.OUT, initial=GPIO.HIGH)

        self._reset()
        self._load_bar_bitmaps()
        self._init_display()

    # ── Public API ────────────────────────────────────────────────────────────

    def write_str(self, position, text):
        """Write a string starting at position (0-indexed). Pads/clips to fit."""
        for i, ch in enumerate(text):
            pos = position + i
            if pos >= self.digits:
                break
            self._write_cmd([_DCRAM_DATA_WRITE | pos, ord(ch)])

    def write_char(self, position, char):
        """Write a single character at position."""
        self._write_cmd([_DCRAM_DATA_WRITE | position, ord(char)])

    def write_custom(self, cgram_address, bitmap):
        """
        Write a custom 5×7 character into CGRAM slot cgram_address (0–7)
        and display it at the same position.

        bitmap : list/bytearray of 5 bytes, one per column, LSB = top row
        """
        self._write_cgram(cgram_address, bitmap)
        self._write_cmd([_DCRAM_DATA_WRITE | cgram_address, cgram_address])

    def clear(self, position=None):
        """Clear one position or the whole display."""
        if position is not None:
            self._write_cmd([_DCRAM_DATA_WRITE | position, _DGRAM_DATA_CLEAR])
        else:
            for i in range(self.digits):
                self._write_cmd([_DCRAM_DATA_WRITE | i, _DGRAM_DATA_CLEAR])

    def set_brightness(self, value):
        """Set brightness 0 (dim) – 255 (full)."""
        self.brightness = max(0, min(255, value))
        self._write_cmd([_SET_DIMMING_DATA, self.brightness])

    def on(self):
        """Wake the display from standby."""
        self._write_cmd([_SET_STAND_BY_OFF, 0x00])

    def off(self):
        """Put the display into standby (low power, screen blank)."""
        self._write_cmd([_SET_STAND_BY_ON, 0x00])

    # ── Scrolling ─────────────────────────────────────────────────────────────

    def scroll_h(self, text, delay=0.15, loops=1, pad=True):
        """
        Scroll text horizontally across the full display.

        text  : string to scroll (any length)
        delay : seconds between each step
        loops : how many times to scroll through (use float('inf') to loop forever)
        pad   : if True, pad with spaces so text fully scrolls off both edges
        """
        if pad:
            padded = " " * self.digits + text + " " * self.digits
        else:
            padded = text

        count = 0
        while count < loops:
            for start in range(len(padded) - self.digits + 1):
                window = padded[start:start + self.digits]
                self.write_str(0, window.ljust(self.digits))
                time.sleep(delay)
            count += 1

    def scroll_v(self, position, chars, delay=0.1, loops=1):
        """
        Scroll characters vertically at a single display position by
        animating a bitmap slide between two characters.

        position : which digit position to animate (0-indexed)
        chars    : string of characters to cycle through, e.g. "ABCDEF"
        delay    : seconds between each row step of the slide
        loops    : how many full cycles through the character list
        """
        if len(chars) < 2:
            return

        # Build a 7-row font lookup from the display's built-in 5×7 patterns.
        # We approximate by using the ASCII dot-matrix for common characters.
        def _char_rows(ch):
            """Return 7 row-bytes for a character (crude built-in font)."""
            return _BUILTIN_FONT.get(ch, _BUILTIN_FONT.get('?', [0x7F] * 7))

        cycle = list(chars) + [chars[0]]  # wrap around
        for _ in range(loops):
            for a, b in zip(cycle, cycle[1:]):
                rows_a = _char_rows(a)
                rows_b = _char_rows(b)
                # Slide b up from the bottom, pushing a off the top
                for step in range(8):
                    combined = rows_a[step:] + rows_b[:step]
                    # Pack 7 rows into a 5-column bitmap
                    bitmap = _rows_to_bitmap(combined[:7])
                    self._write_cgram(7, bitmap)  # use CGRAM slot 7
                    self._write_cmd([_DCRAM_DATA_WRITE | position, 7])
                    time.sleep(delay)

    # ── Blinking ──────────────────────────────────────────────────────────────

    def blink(self, times=3, on_time=0.3, off_time=0.2):
        """
        Blink the entire display by toggling standby.

        times    : number of blink cycles
        on_time  : seconds display is on per cycle
        off_time : seconds display is off per cycle
        """
        for _ in range(times):
            self.off()
            time.sleep(off_time)
            self.on()
            time.sleep(on_time)

    def blink_position(self, position, char, times=3, on_time=0.3, off_time=0.2):
        """
        Blink a single character position by alternating the character with a space.

        position : digit position to blink
        char     : character currently shown there
        """
        for _ in range(times):
            self.write_char(position, ' ')
            time.sleep(off_time)
            self.write_char(position, char)
            time.sleep(on_time)

    # ── Progress bar ──────────────────────────────────────────────────────────

    def progress_bar(self, fraction, prefix='', suffix=''):
        """
        Display a horizontal progress bar across the display.

        fraction : float 0.0 – 1.0 representing how full the bar is
        prefix   : optional single character shown at the far left (e.g. '[')
        suffix   : optional single character shown at the far right (e.g. ']')

        Example:
          vfd.progress_bar(0.5, prefix='[', suffix=']')
          → [▓▓▓▓    ]   (on an 8-digit display with 6 bar cells)
        """
        fraction = max(0.0, min(1.0, fraction))

        # Work out how many character cells the bar occupies
        reserved = len(prefix) + len(suffix)
        bar_cells = self.digits - reserved
        if bar_cells <= 0:
            return

        # Each cell has 5 sub-columns, giving fine-grained resolution
        total_units  = bar_cells * 5
        filled_units = round(fraction * total_units)
        full_cells   = filled_units // 5
        remainder    = filled_units % 5  # 0–4 partial columns in next cell

        pos = 0
        if prefix:
            self.write_char(pos, prefix[0])
            pos += 1

        for i in range(bar_cells):
            if i < full_cells:
                # Fully filled cell — use solid block character
                self._write_cmd([_DCRAM_DATA_WRITE | pos, _BAR_FULL_CHAR])
            elif i == full_cells and remainder > 0:
                # Partial cell — use CGRAM bitmap (slots 1–4)
                self._write_cmd([_DCRAM_DATA_WRITE | pos, remainder])
            else:
                # Empty cell — use CGRAM slot 0 (blank)
                self._write_cmd([_DCRAM_DATA_WRITE | pos, 0])
            pos += 1

        if suffix:
            self.write_char(pos, suffix[0])

    def progress_bar_animated(self, start, end, duration=2.0,
                               prefix='', suffix='', steps=40):
        """
        Animate the progress bar from start to end over duration seconds.

        start    : starting fraction (0.0–1.0)
        end      : ending fraction (0.0–1.0)
        duration : total animation time in seconds
        steps    : number of update steps
        """
        delay = duration / steps
        for i in range(steps + 1):
            frac = start + (end - start) * (i / steps)
            self.progress_bar(frac, prefix=prefix, suffix=suffix)
            time.sleep(delay)

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        """Release GPIO resources."""
        self.clear()
        GPIO.cleanup()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _reset(self):
        GPIO.output(self._rst, GPIO.HIGH)
        time.sleep(0.001)
        GPIO.output(self._rst, GPIO.LOW)
        time.sleep(0.010)
        GPIO.output(self._rst, GPIO.HIGH)
        time.sleep(0.003)

    def _init_display(self):
        self._write_cmd([_SET_DISPLAY_TIMING, self.digits - 1])
        self._write_cmd([_SET_DIMMING_DATA,   self.brightness])
        self._write_cmd([_SET_DISPLAY_ON,     0x00])

    def _load_bar_bitmaps(self):
        """Pre-load progress-bar partial-fill bitmaps into CGRAM slots 0–4."""
        for slot, bitmap in enumerate(_BAR_BITMAPS):
            self._write_cgram(slot, bitmap)

    def _write_cgram(self, address, bitmap):
        """Write a 5-byte bitmap into CGRAM slot address."""
        self._transfer([_CGRAM_DATA_WRITE | address] + list(bitmap))

    def _write_cmd(self, cmd):
        self._transfer(list(cmd))

    def _transfer(self, data):
        """Bit-bang LSB-first SPI transfer."""
        d = self._delay
        GPIO.output(self._cs, GPIO.LOW)
        for byte in data:
            for _ in range(8):
                GPIO.output(self._din, byte & 0x01)  # LSB first
                byte >>= 1
                time.sleep(d)
                GPIO.output(self._clk, GPIO.HIGH)
                time.sleep(d)
                GPIO.output(self._clk, GPIO.LOW)
        GPIO.output(self._cs, GPIO.HIGH)


# ── Vertical scroll helpers ───────────────────────────────────────────────────

def _rows_to_bitmap(rows):
    """Convert 7 row-bytes (each a 5-bit wide row) into a 5-column bitmap."""
    bitmap = [0] * 5
    for col in range(5):
        val = 0
        for row in range(7):
            bit = (rows[row] >> (4 - col)) & 1
            val |= bit << row
        bitmap[col] = val
    return bitmap


# Minimal 5×7 font for vertical scroll (rows, MSB = left column)
_BUILTIN_FONT = {
    ' ': [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    'A': [0x1F, 0x24, 0x44, 0x24, 0x1F, 0x00, 0x00],
    'B': [0x7F, 0x49, 0x49, 0x49, 0x36, 0x00, 0x00],
    'C': [0x3E, 0x41, 0x41, 0x41, 0x22, 0x00, 0x00],
    'D': [0x7F, 0x41, 0x41, 0x22, 0x1C, 0x00, 0x00],
    'E': [0x7F, 0x49, 0x49, 0x49, 0x41, 0x00, 0x00],
    'F': [0x7F, 0x48, 0x48, 0x48, 0x40, 0x00, 0x00],
    'G': [0x3E, 0x41, 0x49, 0x49, 0x2F, 0x00, 0x00],
    'H': [0x7F, 0x08, 0x08, 0x08, 0x7F, 0x00, 0x00],
    'I': [0x41, 0x7F, 0x41, 0x00, 0x00, 0x00, 0x00],
    'J': [0x02, 0x01, 0x41, 0x7E, 0x40, 0x00, 0x00],
    'K': [0x7F, 0x08, 0x14, 0x22, 0x41, 0x00, 0x00],
    'L': [0x7F, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00],
    'M': [0x7F, 0x20, 0x18, 0x20, 0x7F, 0x00, 0x00],
    'N': [0x7F, 0x10, 0x08, 0x04, 0x7F, 0x00, 0x00],
    'O': [0x3E, 0x41, 0x41, 0x41, 0x3E, 0x00, 0x00],
    'P': [0x7F, 0x48, 0x48, 0x48, 0x30, 0x00, 0x00],
    'Q': [0x3E, 0x41, 0x45, 0x42, 0x3D, 0x00, 0x00],
    'R': [0x7F, 0x48, 0x4C, 0x4A, 0x31, 0x00, 0x00],
    'S': [0x32, 0x49, 0x49, 0x49, 0x26, 0x00, 0x00],
    'T': [0x40, 0x40, 0x7F, 0x40, 0x40, 0x00, 0x00],
    'U': [0x7E, 0x01, 0x01, 0x01, 0x7E, 0x00, 0x00],
    'V': [0x7C, 0x02, 0x01, 0x02, 0x7C, 0x00, 0x00],
    'W': [0x7F, 0x02, 0x0C, 0x02, 0x7F, 0x00, 0x00],
    'X': [0x63, 0x14, 0x08, 0x14, 0x63, 0x00, 0x00],
    'Y': [0x60, 0x10, 0x0F, 0x10, 0x60, 0x00, 0x00],
    'Z': [0x43, 0x45, 0x49, 0x51, 0x61, 0x00, 0x00],
    '0': [0x3E, 0x45, 0x49, 0x51, 0x3E, 0x00, 0x00],
    '1': [0x00, 0x21, 0x7F, 0x01, 0x00, 0x00, 0x00],
    '2': [0x23, 0x45, 0x49, 0x49, 0x31, 0x00, 0x00],
    '3': [0x22, 0x41, 0x49, 0x49, 0x36, 0x00, 0x00],
    '4': [0x78, 0x08, 0x08, 0x7F, 0x08, 0x00, 0x00],
    '5': [0x72, 0x51, 0x51, 0x51, 0x4E, 0x00, 0x00],
    '6': [0x1E, 0x29, 0x49, 0x49, 0x06, 0x00, 0x00],
    '7': [0x40, 0x47, 0x48, 0x50, 0x60, 0x00, 0x00],
    '8': [0x36, 0x49, 0x49, 0x49, 0x36, 0x00, 0x00],
    '9': [0x30, 0x49, 0x49, 0x4A, 0x3C, 0x00, 0x00],
    '?': [0x20, 0x40, 0x4D, 0x48, 0x30, 0x00, 0x00],
    '!': [0x00, 0x00, 0x6F, 0x00, 0x00, 0x00, 0x00],
    '.': [0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x00],
    '-': [0x08, 0x08, 0x08, 0x08, 0x08, 0x00, 0x00],
    '%': [0x62, 0x64, 0x08, 0x13, 0x23, 0x00, 0x00],
}


# ── Quick demo ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Change these pin numbers to match your wiring
    with FutabaVFD(din=17, clk=27, cs=22, rst=23, en=24) as vfd:

        print("Horizontal scroll...")
        vfd.scroll_h("HELLO WORLD ", delay=0.15, loops=2)

        print("Vertical scroll at position 0...")
        vfd.clear()
        vfd.write_str(1, "BLINK   ")
        vfd.scroll_v(0, "ABCDE", delay=0.08, loops=1)

        print("Blink whole display...")
        vfd.write_str(0, "BLINK!! ")
        vfd.blink(times=4)

        print("Blink single character...")
        vfd.write_str(0, "HELLO   ")
        vfd.blink_position(0, 'H', times=4)

        print("Progress bar 0 -> 100%...")
        vfd.progress_bar_animated(0.0, 1.0, duration=3.0, prefix='[', suffix=']')

        print("Done.")
        vfd.clear()
