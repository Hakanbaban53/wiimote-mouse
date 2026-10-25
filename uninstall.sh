#!/usr/bin/env bash
# Wii MotionPlus Mouse: uninstall
#
#   ./uninstall.sh              ask, then remove the program (your settings are kept)
#   ./uninstall.sh --yes        do not ask
#   ./uninstall.sh --purge      also delete your settings, saved profiles and state
#   ./uninstall.sh --user-only  leave the system files alone (no sudo)
#   ./uninstall.sh --remove-group   also delete the 'wiimote' group
#   ./uninstall.sh --dry-run    show what would be done
#
# This script does not need the source tree: the installer keeps a copy as `wiimote-mouse-uninstall`.
set -euo pipefail

BIN_DIR="${HOME}/.local/bin"
SHARE_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}"
LIB_DIR="${HOME}/.local/share/wiimote-mouse/lib"
DOC_DIR="${HOME}/.local/share/doc/wiimote-mouse"
APP_DIR="${SHARE_DIR}/applications"
APP_ID="dev.wiimotemouse.Ui"
CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/wiimote-mouse"
STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/wiimote-mouse"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
RULES_DST="/etc/udev/rules.d/70-wiimote-mouse.rules"
MODULES_DST="/etc/modules-load.d/wiimote-mouse.conf"
GROUP="wiimote"

ASSUME_YES=0
PURGE=0
USER_ONLY=0
REMOVE_GROUP=0
DRY_RUN="${WIIMOTE_DRY_RUN:-0}"
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    --purge) PURGE=1 ;;
    --user-only) USER_ONLY=1 ;;
    --remove-group) REMOVE_GROUP=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
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

installed_anything() {
  local path
  for path in "${BIN_DIR}/wiimote-mouse" "${BIN_DIR}/wiimotectl" "${BIN_DIR}/wiimote-mouse-ui" \
              "${LIB_DIR}/wiimote_mouse" "${UNIT_DIR}/wiimote-mouse.service" "${APP_DIR}/${APP_ID}.desktop" \
              "${RULES_DST}" "${MODULES_DST}"; do
    [[ -e "$path" ]] && return 0
  done
  return 1
}

if ! installed_anything && [[ $PURGE -eq 0 ]]; then
  echo "Nothing to remove: Wii MotionPlus Mouse does not look installed."
  exit 0
fi

echo "This will remove Wii MotionPlus Mouse:"
echo "  - the service, the commands (wiimote-mouse, wiimotectl, wiimote-mouse-ui) and the app menu entry"
[[ $USER_ONLY -eq 0 ]] && echo "  - the udev rule and the uinput autoload file (needs sudo)"
if [[ $PURGE -eq 1 ]]; then
  echo "  - YOUR SETTINGS, saved profiles and state (${CONFIG_DIR}, ${STATE_DIR})"
else
  echo "Your settings in ${CONFIG_DIR} are kept (use --purge to delete them)."
fi
[[ $REMOVE_GROUP -eq 1 ]] && echo "  - the '${GROUP}' group"

if [[ $ASSUME_YES -eq 0 && "$DRY_RUN" != "1" ]]; then
  if [[ ! -t 0 ]]; then
    echo "Not running interactively: pass --yes to confirm." >&2
    exit 1
  fi
  read -r -p "Continue? [y/N] " answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) echo "Cancelled."; exit 1 ;;
  esac
fi

say "Stopping the service"
if have_systemd_user; then
  run systemctl --user disable --now wiimote-mouse.service || true
fi

say "Removing user files"
run rm -f "${BIN_DIR}/wiimote-mouse" "${BIN_DIR}/wiimotectl" "${BIN_DIR}/wiimote-mouse-ui" \
  "${UNIT_DIR}/wiimote-mouse.service" "${APP_DIR}/${APP_ID}.desktop"
run rm -rf "${LIB_DIR}/wiimote_mouse" "${DOC_DIR}"
run rmdir "${LIB_DIR}" "${HOME}/.local/share/wiimote-mouse" 2>/dev/null || true
if command -v update-desktop-database >/dev/null 2>&1 && [[ "$DRY_RUN" != "1" ]]; then
  update-desktop-database "${APP_DIR}" >/dev/null 2>&1 || true
fi
if have_systemd_user; then run systemctl --user daemon-reload || true; fi

if [[ $PURGE -eq 1 ]]; then
  say "Deleting your settings and state"
  run rm -rf "${CONFIG_DIR}" "${STATE_DIR}"
fi

if [[ $USER_ONLY -eq 0 ]]; then
  say "Removing system files (needs sudo)"
  as_root rm -f "${RULES_DST}" "${MODULES_DST}"
  as_root udevadm control --reload
  if [[ $REMOVE_GROUP -eq 1 ]] && getent group "${GROUP}" >/dev/null 2>&1; then
    as_root groupdel "${GROUP}"
  fi
fi

# Last: this file may be the installed copy, which is fine to delete while it runs.
run rm -f "${BIN_DIR}/wiimote-mouse-uninstall"

echo
echo "Done."
if [[ $USER_ONLY -eq 0 && $REMOVE_GROUP -eq 0 ]] && getent group "${GROUP}" >/dev/null 2>&1; then
  echo "The '${GROUP}' group and the uinput module were left in place (other software may use them)."
  echo "Remove the group with --remove-group."
fi
