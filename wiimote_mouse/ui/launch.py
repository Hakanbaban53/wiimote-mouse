"""Entry point of the UI that stays importable without GTK, so a missing dependency gives a
helpful message instead of a traceback."""

import sys

UI_DEPENDENCIES = """The UI needs GTK 4, libadwaita and PyGObject. On Arch/CachyOS:
  sudo pacman -S python-gobject gtk4 libadwaita librsvg
Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 librsvg2-common
Fedora:        sudo dnf install python3-gobject gtk4 libadwaita librsvg2"""


def main(argv=None):
    try:
        from . import app
    except (ImportError, ValueError) as exc:
        print(f"wiimote-mouse-ui: cannot start ({exc})\n{UI_DEPENDENCIES}", file=sys.stderr)
        return 1
    return app.main(argv)


if __name__ == "__main__":
    sys.exit(main())
