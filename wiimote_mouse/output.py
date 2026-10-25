"""Virtual input devices (uinput)."""


from evdev import UInput
from evdev import ecodes as E

from . import settings

DEVICE_NAME = "Wii MotionPlus Mouse"
KEYBOARD_NAME = "Wii MotionPlus Keyboard"


class VirtualMouse:
    """uinput device that tracks which buttons are down, so we can always
    release them when the remote goes away."""

    def __init__(self):
        capabilities = {
            E.EV_KEY: [E.BTN_LEFT, E.BTN_RIGHT, E.BTN_MIDDLE],
            E.EV_REL: [E.REL_X, E.REL_Y, E.REL_WHEEL, E.REL_HWHEEL],
        }
        if settings.HIRES_SCROLL:
            capabilities[E.EV_REL].extend([E.REL_WHEEL_HI_RES, E.REL_HWHEEL_HI_RES])
        self._ui = UInput(capabilities, name=DEVICE_NAME)
        self._down = set()

    def button(self, code, down):
        if down == (code in self._down):
            return
        (self._down.add if down else self._down.discard)(code)
        self._ui.write(E.EV_KEY, code, int(down))
        self._ui.syn()

    def release_all(self):
        for code in sorted(self._down):
            self._ui.write(E.EV_KEY, code, 0)
        if self._down:
            self._ui.syn()
        self._down.clear()

    def emit(self, dx=0, dy=0, hires=0, steps=0):
        """Pointer motion and/or vertical wheel in a single report."""
        wrote = False
        if dx or dy:
            self._ui.write(E.EV_REL, E.REL_X, dx)
            self._ui.write(E.EV_REL, E.REL_Y, dy)
            wrote = True
        wrote |= self._wheel(E.REL_WHEEL, steps, hires)
        if wrote:
            self._ui.syn()

    def scroll(self, wheel, steps, hires=0):
        if self._wheel(wheel, steps, hires):
            self._ui.syn()

    def _wheel(self, wheel, steps, hires):
        """Matched high-res and legacy scroll; one pair is one scroll source."""
        wrote = False
        highres_code = E.REL_WHEEL_HI_RES if wheel == E.REL_WHEEL else E.REL_HWHEEL_HI_RES
        if settings.HIRES_SCROLL and hires:
            self._ui.write(E.EV_REL, highres_code, hires)
            wrote = True
        if steps:
            self._ui.write(E.EV_REL, wheel, steps)
            wrote = True
        return wrote

    def close(self):
        try:
            self.release_all()
        except OSError:
            pass
        self._ui.close()


def key_codes():
    """Every KEY_* code the virtual keyboard can send (BTN_* are not keyboard keys)."""
    codes = set()
    for code, names in E.KEY.items():
        names = names if isinstance(names, (list, tuple)) else [names]
        if 0 < code <= 0x2FF and any(name.startswith("KEY_") for name in names):
            codes.add(code)
    return sorted(codes)


class VirtualKeyboard:
    """uinput keyboard for button profiles (presentation keys, media keys, push-to-talk).

    Tracks held keys so they can always be released when the remote goes away or the
    profile changes, and counts presses so two bindings sharing a key don't fight.
    """

    def __init__(self):
        self._ui = UInput({E.EV_KEY: key_codes()}, name=KEYBOARD_NAME)
        self._held = {}

    def press(self, codes):
        for code in codes:
            self._held[code] = self._held.get(code, 0) + 1
            if self._held[code] == 1:
                self._ui.write(E.EV_KEY, code, 1)
        self._ui.syn()

    def release(self, codes):
        for code in reversed(codes):
            count = self._held.get(code, 0)
            if count <= 0:
                continue
            if count == 1:
                del self._held[code]
                self._ui.write(E.EV_KEY, code, 0)
            else:
                self._held[code] = count - 1
        self._ui.syn()

    def tap(self, codes):
        self.press(codes)
        self.release(codes)

    def release_all(self):
        for code in sorted(self._held):
            self._ui.write(E.EV_KEY, code, 0)
        if self._held:
            self._ui.syn()
        self._held.clear()

    @property
    def held(self):
        return sorted(self._held)

    def close(self):
        try:
            self.release_all()
        except OSError:
            pass
        self._ui.close()
