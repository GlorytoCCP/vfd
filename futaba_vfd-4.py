"""
Futaba 8-MD-06INKM VFD Display Driver for Raspberry Pi
=======================================================
Soft (bit-banged) SPI — use any GPIO pins you like.

Character codes are single bytes:
  0x00–0x07  : CGRAM slots (custom characters, 8 available)
  0x20–0x7F  : Standard ASCII
  0x80–0xFF  : Extended built-in symbols (see SYMBOLS dict below)

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

Basic usage:
  from futaba_vfd import FutabaVFD, SYMBOLS
  vfd = FutabaVFD(din=17, clk=27, cs=22, rst=23, en=24)
  vfd.write_str(0, "HELLO   ")
  vfd.write_code(4, SYMBOLS['PLAY'])
  vfd.scroll_h("HELLO WORLD", delay=0.15)
  vfd.scroll_v(0, "AB", delay=0.1)
  vfd.blink(times=3)
  vfd.progress_bar(0.75, prefix='[', suffix=']')
  vfd.set_brightness(200)
  vfd.clear()
  vfd.close()
"""

import time
import RPi.GPIO as GPIO

# ── Command bytes ─────────────────────────────────────────────────────────────
_DCRAM_DATA_WRITE   = 0x20  # write character to display RAM at position
_DGRAM_DATA_CLEAR   = 0x10  # blank a single position
_CGRAM_DATA_WRITE   = 0x40  # write custom 5×7 bitmap to CGRAM slot
_SET_DISPLAY_TIMING = 0xE0  # set number of active digits
_SET_DIMMING_DATA   = 0xE4  # set brightness (0–255)
_SET_DISPLAY_ON     = 0xE8  # wake display
_SET_DISPLAY_OFF    = 0xEA  # all segments off
_SET_STAND_BY_ON    = 0xEC  # standby (low power)
_SET_STAND_BY_OFF   = 0xED  # leave standby

# ── Built-in symbol codes ──────────────────────────────────────────────────
# Verified against actual hardware using the scan_chars.py script.
SYMBOLS = {
    # Horizontal partial-fill blocks (for progress bars) — 6 levels
    'BLOCKh_FULL'     : 0x15,
    'BLOCKh_0.8'      : 0x14,
    'BLOCKh_0.6'      : 0x13,
    'BLOCKh_0.4'      : 0x12,
    'BLOCKh_0.2'      : 0x11,
    'BLANK'           : 0x10,

    # Vertical partial-fill blocks — 7 levels
    'BLOCKv_1/7'      : 0x1B,
    'BLOCKv_2/7'      : 0x1A,
    'BLOCKv_3/7'      : 0x19,
    'BLOCKv_4/7'      : 0x18,
    'BLOCKv_5/7'      : 0x17,
    'BLOCKv_6/7'      : 0x16,

    'CHECKERED-'      : 0x1D,
    'CHECKERED+'      : 0x1E,

    # Arrows
    'ARROW_RIGHT'     : 0x0B,  # ▶ right pointing (play-like)
    'ARROW_LEFT'      : 0x0A,  # ◀ left pointing
    'ARROW_UP'        : 0x08,  # ▲ up
    'ARROW_DOWN'      : 0x09,  # ▼ down
    'ARROW_DL'        : 0xFA,  # detailed arrow left
    'ARROW_DR'        : 0xF9,  # detailed arrow right
    'ARROW_DU'        : 0xCB,  # detailed arrow up
    'ARROW_DD'        : 0xCC,  # detailed arrow down
    'GOOD_ARROWL'     : 0xAE,
    'GOOD_ARROWR'     : 0xAF,
    'WEIRD_ARROWL'    : 0x0C,
    'WEIRD_ARROWR'    : 0x0D,

    # Media controls
    'PLAY'            : 0xFD,
    'PAUSE'           : 0x0F,
}



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
        spi_delay  : soft SPI clock half-period in seconds
        """
        self.digits      = digits
        self.brightness  = brightness
        self._delay      = spi_delay
        self._din        = din
        self._clk        = clk
        self._cs         = cs
        self._rst        = rst
        self._en         = en

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in (din, clk, cs, rst):
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
        if en is not None:
            GPIO.setup(en, GPIO.OUT, initial=GPIO.HIGH)

        self._reset()
        self._init_display()

    # ── Public API ────────────────────────────────────────────────────────────

    def write_str(self, position, text):
        """Write a string starting at position (0-indexed)."""
        for i, ch in enumerate(text):
            pos = position + i
            if pos >= self.digits:
                break
            self._write_cmd([_DCRAM_DATA_WRITE | pos, ord(ch)])

    def write_char(self, position, char):
        """Write a single ASCII character at position."""
        self._write_cmd([_DCRAM_DATA_WRITE | position, ord(char)])

    def write_code(self, position, code):
        """
        Write a character by its raw byte code at position.
        Use this for extended symbols and CGRAM slots.

        Examples:
          vfd.write_code(0, 0xDF)              # degree symbol
          vfd.write_code(0, SYMBOLS['PLAY'])   # play symbol
          vfd.write_code(0, 0x00)              # CGRAM slot 0
        """
        self._write_cmd([_DCRAM_DATA_WRITE | position, code])

    def write_symbol(self, position, name):
        """
        Write a named symbol at position.
        Name must be a key from the SYMBOLS dictionary.

        Example:
          vfd.write_symbol(0, 'PLAY')
          vfd.write_symbol(1, 'PLAY')
        """
        if name not in SYMBOLS:
            raise KeyError(f"Unknown symbol '{name}'. See SYMBOLS dict for available names.")
        self._write_cmd([_DCRAM_DATA_WRITE | position, SYMBOLS[name]])

    def write_custom(self, slot, bitmap, position=None):
        """
        Write a custom 5×7 character bitmap into a CGRAM slot and optionally
        display it at a position.

        slot     : CGRAM slot 0–7 (slots 0–3 reserved if using progress_bar)
        bitmap   : list of 5 bytes, one per column, LSB = top row
        position : display position to show it at, or None to just load it

        Example — a heart symbol:
          heart = [0x0E, 0x1F, 0x1F, 0x0E, 0x04]
          vfd.write_custom(4, heart, position=0)
        """
        self._write_cgram(slot, bitmap)
        if position is not None:
            self._write_cmd([_DCRAM_DATA_WRITE | position, slot])

    def show_custom(self, position, slot):
        """
        Display a previously loaded CGRAM slot at a position.

        Example:
          vfd.write_custom(4, my_bitmap)   # load into slot 4
          vfd.show_custom(0, 4)            # show at position 0
          vfd.show_custom(1, 4)            # show at position 1 too
        """
        self._write_cmd([_DCRAM_DATA_WRITE | position, slot])

    def clear(self, position=None):
        """Clear one position or the whole display."""
        if position is not None:
            self._write_cmd([_DCRAM_DATA_WRITE | position, 0x20])  # space
        else:
            for i in range(self.digits):
                self._write_cmd([_DCRAM_DATA_WRITE | i, 0x20])

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
        loops : how many times to scroll through (float('inf') to loop forever)
        pad   : if True, text scrolls fully on and off both edges
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
        Animate characters sliding vertically at a single display position.
        Uses CGRAM slot 7 temporarily during animation.

        position : which digit position to animate (0-indexed)
        chars    : string of characters to cycle through e.g. "ABCDE"
        delay    : seconds between each row step
        loops    : how many full cycles through the character list
        """
        if len(chars) < 2:
            return

        def _char_rows(ch):
            return _BUILTIN_FONT.get(ch, _BUILTIN_FONT.get('?', [0x7F] * 7))

        cycle = list(chars) + [chars[0]]
        for _ in range(loops):
            for a, b in zip(cycle, cycle[1:]):
                rows_a = _char_rows(a)
                rows_b = _char_rows(b)
                for step in range(8):
                    combined = rows_a[step:] + rows_b[:step]
                    bitmap = _rows_to_bitmap(combined[:7])
                    self._write_cgram(7, bitmap)
                    self._write_cmd([_DCRAM_DATA_WRITE | position, 7])
                    time.sleep(delay)

    # ── Blinking ──────────────────────────────────────────────────────────────

    def blink(self, times=3, on_time=0.3, off_time=0.2):
        """Blink the entire display by toggling standby."""
        for _ in range(times):
            self.off()
            time.sleep(off_time)
            self.on()
            time.sleep(on_time)

    def blink_position(self, position, code, times=3, on_time=0.3, off_time=0.2):
        """
        Blink a single position by alternating between a character and a space.

        position : digit position to blink
        code     : byte code of the character currently shown (use ord('A') for ASCII,
                   or a raw code like 0xDF for symbols)
        """
        for _ in range(times):
            self._write_cmd([_DCRAM_DATA_WRITE | position, 0x20])  # space
            time.sleep(off_time)
            self._write_cmd([_DCRAM_DATA_WRITE | position, code])
            time.sleep(on_time)

    # ── Progress bars ─────────────────────────────────────────────────────────

    # Horizontal fill levels, ordered from emptiest to fullest.
    # Each display cell can show one of 6 levels using built-in symbols.
    _BARH_LEVELS = ['BLANK', 'BLOCKh_0.2', 'BLOCKh_0.4',
                    'BLOCKh_0.6', 'BLOCKh_0.8', 'BLOCKh_FULL']

    def progress_bar(self, fraction, prefix='', suffix=''):
        """
        Display a horizontal progress bar across the display using the
        built-in partial-fill block characters (6 levels per cell: blank,
        20%, 40%, 60%, 80%, full).

        fraction : float 0.0–1.0
        prefix   : optional single character at far left  e.g. '['
        suffix   : optional single character at far right e.g. ']'

        Examples:
          vfd.progress_bar(0.5)
          vfd.progress_bar(0.75, prefix='[', suffix=']')
        """
        fraction  = max(0.0, min(1.0, fraction))
        reserved  = len(prefix) + len(suffix)
        bar_cells = self.digits - reserved
        if bar_cells <= 0:
            return

        levels_per_cell = len(self._BARH_LEVELS) - 1  # 5 steps between levels
        total_units     = bar_cells * levels_per_cell
        filled_units    = round(fraction * total_units)
        full_cells      = filled_units // levels_per_cell
        remainder       = filled_units % levels_per_cell  # 0–4 -> partial level index

        pos = 0
        if prefix:
            self.write_char(pos, prefix[0])
            pos += 1

        for i in range(bar_cells):
            if i < full_cells:
                level_name = 'BLOCKh_FULL'
            elif i == full_cells and remainder > 0:
                level_name = self._BARH_LEVELS[remainder]
            else:
                level_name = 'BLANK'
            self._write_cmd([_DCRAM_DATA_WRITE | pos, SYMBOLS[level_name]])
            pos += 1

        if suffix:
            self.write_char(pos, suffix[0])

    def progress_bar_animated(self, start, end, duration=2.0,
                               prefix='', suffix='', steps=40):
        """
        Animate the progress bar from start to end over duration seconds,
        with each cell smoothly cycling through its fill levels in turn.

        start    : starting fraction (0.0–1.0)
        end      : ending fraction (0.0–1.0)
        duration : total animation time in seconds
        steps    : number of update steps (more = smoother)
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
        """Clear the display and release GPIO resources."""
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

    def _write_cgram(self, address, bitmap):
        self._transfer([_CGRAM_DATA_WRITE | address] + list(bitmap))

    def _write_cmd(self, cmd):
        self._transfer(list(cmd))

    def _transfer(self, data):
        """Bit-bang LSB-first SPI transfer."""
        d = self._delay
        GPIO.output(self._cs, GPIO.LOW)
        for byte in data:
            for _ in range(8):
                GPIO.output(self._din, byte & 0x01)
                byte >>= 1
                time.sleep(d)
                GPIO.output(self._clk, GPIO.HIGH)
                time.sleep(d)
                GPIO.output(self._clk, GPIO.LOW)
        GPIO.output(self._cs, GPIO.HIGH)


# ── Vertical scroll helpers ───────────────────────────────────────────────────

def _rows_to_bitmap(rows):
    bitmap = [0] * 5
    for col in range(5):
        val = 0
        for row in range(7):
            bit = (rows[row] >> (4 - col)) & 1
            val |= bit << row
        bitmap[col] = val
    return bitmap


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
    from futaba_vfd import FutabaVFD, SYMBOLS

    with FutabaVFD(din=21, clk=20, cs=16, rst=26, en=19) as vfd:

        print("Writing text...")
        vfd.write_str(0, "HELLO   ")
        time.sleep(1)

        print("Writing symbol by name...")
        vfd.write_symbol(7, 'PLAY')
        time.sleep(1)

        print("Writing symbol by raw code...")
        vfd.write_code(6, 0xDF)
        time.sleep(1)

        print("Custom character (heart)...")
        heart = [0x0E, 0x1F, 0x1F, 0x0E, 0x04]
        vfd.write_custom(4, heart, position=0)
        time.sleep(1)

        print("Horizontal scroll...")
        vfd.scroll_h("HELLO WORLD ", delay=0.15, loops=2)

        print("Vertical scroll...")
        vfd.clear()
        vfd.scroll_v(0, "ABCDE", delay=0.08, loops=1)

        print("Blink display...")
        vfd.write_str(0, "BLINK!! ")
        vfd.blink(times=3)

        print("Blink single position...")
        vfd.blink_position(0, ord('B'), times=3)

        print("Progress bar with brackets...")
        vfd.progress_bar_animated(0.0, 1.0, duration=3.0, prefix='[', suffix=']')

        print("Full width progress bar...")
        vfd.progress_bar_animated(0.0, 1.0, duration=2.0)

        print("Done.")
        vfd.clear()
