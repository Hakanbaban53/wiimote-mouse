#!/usr/bin/env bash
# Wii MotionPlus Mouse: install / uninstall
#
#   ./install.sh                      install (user files, plus system files via sudo)
#   ./install.sh --user-only          user files only (no sudo; install the udev rule yourself)
#   ./install.sh --no-start           do not enable or start the service
#   ./uninstall.sh                    remove it again (see ./uninstall.sh --help)
#   WIIMOTE_DRY_RUN=1 ./install.sh    show the commands without running anything
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
LIB_DIR="${HOME}/.local/share/wiimote-mouse/lib"
DOC_DIR="${HOME}/.local/share/doc/wiimote-mouse"
APP_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
APP_ID="dev.wiimotemouse.Ui"
CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/wiimote-mouse"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
RULES_DST="/etc/udev/rules.d/70-wiimote-mouse.rules"
MODULES_DST="/etc/modules-load.d/wiimote-mouse.conf"
GROUP="wiimote"

# `./install.sh uninstall [options]` still works: it hands over to uninstall.sh.
if [[ "${1:-}" == "uninstall" ]]; then
  shift
  exec bash "${HERE}/uninstall.sh" "$@"
fi

USER_ONLY=0
NO_START=0
for arg in "$@"; do
  case "$arg" in
    install) ;;
    --user-only) USER_ONLY=1 ;;
    --no-start) NO_START=1 ;;
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

run() {
  if [[ "${WIIMOTE_DRY_RUN:-0}" == "1" ]]; then
    printf '   [dry run] %q' "$1"; shift
    printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

as_root() {
  if [[ $EUID -eq 0 ]]; then run "$@"; else run sudo "$@"; fi
}

have_systemd_user() {
  command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

# A launcher is a tiny script that points Python at the installed package.
write_launcher() {
  local name="$1" module="$2"
  if [[ "${WIIMOTE_DRY_RUN:-0}" == "1" ]]; then
    printf '   [dry run] write launcher %s\n' "${BIN_DIR}/${name}"
    return
  fi
  mkdir -p "${BIN_DIR}"
  cat > "${BIN_DIR}/${name}" <<LAUNCHER
#!/usr/bin/env python3
import sys

sys.path.insert(0, "${LIB_DIR}")
from wiimote_mouse.${module} import main

sys.exit(main())
LAUNCHER
  chmod 755 "${BIN_DIR}/${name}"
}

install_desktop_entry() {
  local target="${APP_DIR}/${APP_ID}.desktop"
  if [[ "${WIIMOTE_DRY_RUN:-0}" == "1" ]]; then
    printf '   [dry run] write desktop entry %s\n' "${target}"
    return
  fi
  mkdir -p "${APP_DIR}"
  sed "s|@BIN_DIR@|${BIN_DIR}|g" "${HERE}/packaging/${APP_ID}.desktop.in" > "${target}"
  chmod 644 "${target}"
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${APP_DIR}" >/dev/null 2>&1 || true
  fi
}

install_all() {
  say "Checking Python and python-evdev"
  if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    echo "Python 3.9 or newer is required." >&2
    exit 1
  fi
  if ! python3 -c 'import evdev' 2>/dev/null; then
    cat >&2 <<MSG
python-evdev was not found. Install it for your distribution:
  Arch/CachyOS : sudo pacman -S python-evdev
  Debian/Ubuntu: sudo apt install python3-evdev
  Fedora       : sudo dnf install python3-evdev
MSG
    exit 1
  fi

  say "Installing user files"
  run rm -rf "${LIB_DIR}/wiimote_mouse"
  run mkdir -p "${LIB_DIR}"
  run cp -r "${HERE}/wiimote_mouse" "${LIB_DIR}/wiimote_mouse"
  run find "${LIB_DIR}" -name __pycache__ -type d -prune -exec rm -rf {} +
  write_launcher wiimote-mouse daemon
  write_launcher wiimotectl ctl
  write_launcher wiimote-mouse-ui ui.launch
  install_desktop_entry
  run install -Dm755 "${HERE}/uninstall.sh" "${BIN_DIR}/wiimote-mouse-uninstall"
  run install -Dm644 "${HERE}/packaging/wiimote-mouse.service" "${UNIT_DIR}/wiimote-mouse.service"
  run install -Dm644 "${HERE}/README.md" "${DOC_DIR}/README.md"
  run install -Dm644 "${HERE}/config.example.toml" "${DOC_DIR}/config.example.toml"
  if [[ -e "${CONFIG_DIR}/config.toml" ]]; then
    say "Keeping your existing configuration: ${CONFIG_DIR}/config.toml"
  else
    run install -Dm644 "${HERE}/config.example.toml" "${CONFIG_DIR}/config.toml"
  fi

  if [[ $USER_ONLY -eq 0 ]]; then
    say "Installing system files (needs sudo)"
    as_root install -Dm644 "${HERE}/packaging/70-wiimote-mouse.rules" "${RULES_DST}"
    as_root install -Dm644 "${HERE}/packaging/wiimote-mouse-modules.conf" "${MODULES_DST}"
    if ! getent group "${GROUP}" >/dev/null 2>&1; then
      as_root groupadd --system "${GROUP}"
    fi
    if ! id -nG "$(id -un)" | tr ' ' '\n' | grep -qx "${GROUP}"; then
      as_root usermod -aG "${GROUP}" "$(id -un)"
      NEED_RELOGIN=1
    fi
    as_root modprobe uinput
    as_root udevadm control --reload
    as_root udevadm trigger --action=add --subsystem-match=input --subsystem-match=misc --subsystem-match=leds
  else
    say "--user-only: skipped the udev rule and the uinput module (install the files in packaging/ yourself)"
  fi

  if [[ $NO_START -eq 0 ]] && have_systemd_user; then
    say "Enabling the service"
    run systemctl --user daemon-reload
    run systemctl --user enable --now wiimote-mouse.service
    run systemctl --user restart wiimote-mouse.service
  else
    say "Service not started (--no-start, or there is no user systemd session)"
  fi

  echo
  echo "Done. Check the setup with:  ${BIN_DIR}/wiimote-mouse --doctor"
  echo "Control it with:             ${BIN_DIR}/wiimotectl --help"
  echo "Remove it again with:        wiimote-mouse-uninstall"
  if python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' 2>/dev/null; then
    echo "Open the UI with:            wiimote-mouse-ui   (or \"Wii Remote Mouse\" in your app menu)"
  else
    echo "The UI needs GTK 4 + libadwaita (optional). Arch/CachyOS: sudo pacman -S python-gobject gtk4 libadwaita librsvg"
  fi
  if [[ "${NEED_RELOGIN:-0}" == "1" ]]; then
    echo "NOTE: you were added to the '${GROUP}' group; log out and back in for LED feedback to work."
  fi
  case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) echo "NOTE: ${BIN_DIR} is not on your PATH." ;;
  esac
}

install_all
