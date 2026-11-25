#!/usr/bin/env bash
# One-time root setup of the box's Pi for the pi-ops deployment (docs/DEPLOY.md).
#
#   sudo ~/apps/daily-panel/deploy/host-setup.sh              # or from any checkout
#   sudo deploy/host-setup.sh --user bash102 --no-apt
#
# pi-ops' git-deploy clones the repo, builds .venv and runs app-zozobox.service
# as the service user. This script does what git-deploy cannot:
#   - apt packages (audio tools, ffmpeg, libusb, fonts, i2c-tools, python3-lgpio)
#   - enable I2C (the PCA9685 LED driver)
#   - add the service user to gpio/i2c/audio/lp/plugdev (plain Debian: create
#     gpio/i2c and a udev rule for /dev/gpiochip* and /dev/i2c-*)
#   - the QL-800 udev rule, /etc/asound.conf (softvol hard caps)
#   - /etc/sudoers.d/zozobox-poweroff: a rule for exactly
#     `/usr/bin/systemctl poweroff` (the parent chord). It confines nothing
#     under pi-ops, whose bash102 already has passwordless sudo systemctl.
#   - ~/.config/zozobox (700) with box.toml and env (600) from the templates,
#     and ~/.local/share/zozobox (700)
#
# Options:
#   --user NAME   the pi-ops service user (default: $SUDO_USER, else bash102).
#                 pi-ops itself hardcodes bash102 (its bootstrap.sh and units),
#                 so on a pi-ops Pi this is always bash102; anything else warns.
#   --no-apt      skip apt-get
#
# Idempotent: safe to re-run. It never overwrites ~/.config/zozobox/box.toml,
# ~/.config/zozobox/env or an existing /etc/asound.conf (a changed template is
# written beside it as asound.conf.zozobox-new).
set -euo pipefail

SERVICE_USER=${SUDO_USER:-bash102}
[[ $SERVICE_USER == root ]] && SERVICE_USER=bash102
DO_APT=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) SERVICE_USER=${2:?--user needs a name}; shift 2 ;;
    --user=*) SERVICE_USER=${1#--user=}; shift ;;
    --no-apt) DO_APT=0; shift ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

APT_PACKAGES=(python3-venv python3-dev python3-lgpio alsa-utils ffmpeg libusb-1.0-0
              fonts-dejavu-core i2c-tools)
GROUPS_WANTED=(gpio i2c audio lp plugdev)

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }

[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0" >&2; exit 1; }
[[ $SERVICE_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "bad user name: $SERVICE_USER" >&2; exit 2; }
id -u "$SERVICE_USER" >/dev/null 2>&1 || {
  echo "no user $SERVICE_USER: pass the pi-ops service user with --user" >&2; exit 1; }
USER_HOME=$(getent passwd "$SERVICE_USER" | cut -d: -f6)
USER_GROUP=$(id -gn "$SERVICE_USER")
[[ -d $USER_HOME ]] || { echo "home of $SERVICE_USER ($USER_HOME) does not exist" >&2; exit 1; }
DEPLOY=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[[ -f $DEPLOY/pi.toml && -f $DEPLOY/env.example ]] || { echo "not a zozobox deploy/ dir: $DEPLOY" >&2; exit 1; }
as_user() { runuser -u "$SERVICE_USER" -- "$@"; }
REBOOT=0
echo "service user: $SERVICE_USER (home $USER_HOME)"
if [[ $SERVICE_USER != bash102 ]]; then
  warn "pi-ops runs git-deploy and every app unit as bash102 (hardcoded in its bootstrap.sh and units); $SERVICE_USER only works with a modified pi-ops"
fi

# -- packages -----------------------------------------------------------------
if [[ $DO_APT -eq 1 ]]; then
  say "apt packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  if ! apt-get install -y -q "${APT_PACKAGES[@]}"; then
    # One by one, so a package missing on this distribution (python3-lgpio is
    # in Raspberry Pi OS; plain Debian may lack it) does not block the rest.
    missing=()
    for p in "${APT_PACKAGES[@]}"; do
      apt-get install -y -q "$p" >/dev/null 2>&1 || missing+=("$p")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
      warn "not installed: ${missing[*]}"
    fi
    if [[ " ${missing[*]} " == *" python3-lgpio "* ]]; then
      # gpiozero's pin factory then comes from pip: the [pi] extra lists lgpio,
      # which builds from source: a C compiler, swig and the lgpio C library.
      warn "python3-lgpio unavailable: pip will build lgpio (installing build-essential swig liblgpio-dev)"
      apt-get install -y -q build-essential swig liblgpio-dev ||
        warn "build-essential/swig/liblgpio-dev unavailable: install a compiler and swig, build liblgpio from https://github.com/joan2937/lg (make && sudo make install), then as $SERVICE_USER: git-deploy unblock daily-panel && git-deploy sync daily-panel (a failed install blocks the app; the unit does not exist before the first good install)"
    fi
  fi
fi

# -- I2C (PCA9685 LED driver) -----------------------------------------------------
say "I2C"
if command -v raspi-config >/dev/null 2>&1; then
  if [[ $(raspi-config nonint get_i2c 2>/dev/null || echo 1) != 0 ]]; then
    raspi-config nonint do_i2c 0 && REBOOT=1 || warn "raspi-config could not enable I2C"
  else
    echo "  already enabled"
  fi
else
  # Plain Debian. The block is appended in its own [all] section after an
  # empty dtoverlay=: a dtparam= that follows a dtoverlay= line applies to
  # that overlay, and one inside a [pi4]/[cm4] filter only to that model.
  # Debian's raspi-firmware regenerates config.txt on kernel upgrades and
  # appends /etc/default/raspi-firmware-custom, so the block goes there too.
  I2C_MARK='# Zozo box: PCA9685 LED driver (deploy/host-setup.sh)'
  cfg=""
  for c in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f $c ]] && { cfg=$c; break; }
  done
  targets=()
  if [[ -f /etc/default/raspi-firmware-custom ]] ||
     dpkg-query -W -f='${Status}' raspi-firmware 2>/dev/null | grep -q 'ok installed'; then
    targets+=(/etc/default/raspi-firmware-custom)
  fi
  [[ -n $cfg ]] && targets+=("$cfg")
  if [[ -e /dev/i2c-1 ]]; then
    echo "  already enabled (/dev/i2c-1 exists)"
  elif [[ ${#targets[@]} -eq 0 ]]; then
    warn "no config.txt in /boot/firmware or /boot: enable I2C yourself (dtparam=i2c_arm=on in an [all] section)"
  else
    for f in "${targets[@]}"; do
      if [[ -f $f ]] && grep -qF "$I2C_MARK" "$f"; then
        echo "  $f already has the I2C block (reboot if /dev/i2c-1 is still missing)"
      else
        printf '\n%s\n[all]\ndtoverlay=\ndtparam=i2c_arm=on\n' "$I2C_MARK" >> "$f"
        echo "  added the I2C block ([all], dtoverlay=, dtparam=i2c_arm=on) to $f"
      fi
    done
    REBOOT=1
  fi
  echo i2c-dev > /etc/modules-load.d/zozobox-i2c.conf
  modprobe i2c-dev 2>/dev/null || true
fi

# -- groups (and plain-Debian device permissions) ------------------------------------
say "groups for $SERVICE_USER"
PI_OS_RULES=0
for r in /etc/udev/rules.d/99-com.rules /lib/udev/rules.d/99-com.rules /usr/lib/udev/rules.d/99-com.rules; do
  [[ -e $r ]] && PI_OS_RULES=1
done
if [[ $PI_OS_RULES -eq 0 ]]; then
  # Plain Debian: nobody grants gpio/i2c access to a group. Create the groups
  # and a rule for /dev/gpiochip* and /dev/i2c-*.
  for g in gpio i2c; do
    getent group "$g" >/dev/null || groupadd --system "$g"
  done
  install -m 0644 "$DEPLOY/udev/99-zozobox-gpio-i2c.rules" /etc/udev/rules.d/99-zozobox-gpio-i2c.rules
  echo "  installed /etc/udev/rules.d/99-zozobox-gpio-i2c.rules"
fi
added=()
for g in "${GROUPS_WANTED[@]}"; do
  if getent group "$g" >/dev/null; then
    id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx "$g" || { usermod -aG "$g" "$SERVICE_USER"; added+=("$g"); }
  else
    warn "group $g does not exist; skipped"
  fi
done
[[ ${#added[@]} -gt 0 ]] && echo "  added to: ${added[*]}" || echo "  already in every group"

# -- udev: QL-800 ----------------------------------------------------------------------
say "udev rules"
install -m 0644 "$DEPLOY/udev/99-zozobox-ql800.rules" /etc/udev/rules.d/99-zozobox-ql800.rules
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=usb --attr-match=idVendor=04f9 2>/dev/null || true
udevadm trigger --subsystem-match=gpio --subsystem-match=i2c-dev 2>/dev/null || true

# -- ALSA: softvol hard caps -------------------------------------------------------------
say "ALSA (/etc/asound.conf)"
if [[ ! -e /etc/asound.conf ]]; then
  install -m 0644 "$DEPLOY/asound.conf" /etc/asound.conf
  echo "  installed /etc/asound.conf (card names are placeholders: docs/BRINGUP.md)"
elif cmp -s "$DEPLOY/asound.conf" /etc/asound.conf; then
  echo "  /etc/asound.conf is up to date"
else
  install -m 0644 "$DEPLOY/asound.conf" /etc/asound.conf.zozobox-new
  echo "  kept /etc/asound.conf; the current template is /etc/asound.conf.zozobox-new"
fi

# -- sudoers: the poweroff ------------------------------------------------------------------
# Only matters where the service user has no broader sudo. Under pi-ops it
# already has passwordless sudo for systemctl (git-deploy restarts units from
# the webhook and timers), which is root-equivalent: see docs/DEPLOY.md.
say "sudoers drop-in (poweroff, for $SERVICE_USER)"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
sed "s/^SERVICE_USER /$SERVICE_USER /" "$DEPLOY/sudoers/zozobox-poweroff" > "$tmp"
chmod 0440 "$tmp"
if grep -qx "$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff" "$tmp" &&
   visudo -cf "$tmp" >/dev/null; then
  install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/zozobox-poweroff
  echo "  installed /etc/sudoers.d/zozobox-poweroff"
else
  warn "sudoers drop-in failed visudo; not installed (the parent chord cannot power off)"
fi

# -- the service user's config and data ------------------------------------------------------
say "config and data dirs"
CONF=$USER_HOME/.config/zozobox
DATA=$USER_HOME/.local/share/zozobox
as_user mkdir -p "$USER_HOME/.config" "$USER_HOME/.local/share"
as_user mkdir -p "$CONF" "$DATA"
chown "$SERVICE_USER:$USER_GROUP" "$CONF" "$DATA"
chmod 0700 "$CONF" "$DATA"
if [[ ! -e $CONF/box.toml ]]; then
  install -m 0644 -o "$SERVICE_USER" -g "$USER_GROUP" "$DEPLOY/box.toml.example" "$CONF/box.toml"
  echo "  created $CONF/box.toml (edit pins, cards, ceilings during bring-up)"
else
  echo "  kept $CONF/box.toml"
fi
if [[ ! -e $CONF/env ]]; then
  install -m 0600 -o "$SERVICE_USER" -g "$USER_GROUP" "$DEPLOY/env.example" "$CONF/env"
  echo "  created $CONF/env from the template: FILL IT IN"
else
  chown "$SERVICE_USER:$USER_GROUP" "$CONF/env"
  chmod 0600 "$CONF/env"
  echo "  kept $CONF/env (mode 600)"
fi
PLACEHOLDER_RE='^[A-Z_]+=(replace-me|.*example\.workers\.dev|.*<)'
if grep -Eq "$PLACEHOLDER_RE" "$CONF/env"; then
  warn "$CONF/env still has placeholders:"
  grep -E "$PLACEHOLDER_RE" "$CONF/env" | cut -d= -f1 | sed 's/^/    /' >&2
fi

# -- leftovers of the old standalone install ---------------------------------------------------
if compgen -G '/etc/systemd/system/zozobox-*' >/dev/null; then
  warn "the old standalone units are installed; they would fight app-zozobox for the hardware:"
  echo "    sudo systemctl disable --now zozobox-engine zozobox-dashboard zozobox-sync zozobox-nightly.timer zozobox-mood-reprocess.timer"
  echo "    sudo rm /etc/systemd/system/zozobox-*; sudo systemctl daemon-reload"
fi

# -- clock ---------------------------------------------------------------------------------
# No RTC on a Pi 3: the supervisor holds nightly/bootstrap until NTP has set the clock.
if command -v timedatectl >/dev/null 2>&1; then
  if [[ $(timedatectl show -p NTPSynchronized --value 2>/dev/null) != yes ]]; then
    warn "the clock is not NTP-synchronized (timedatectl: NTPSynchronized=no). Make sure an NTP client (systemd-timesyncd or chrony) runs: until it syncs, the nightly run and the bootstrap wait"
  fi
fi

say "done"
if [[ $REBOOT -eq 1 ]]; then
  echo "  Reboot once so that I2C is enabled: sudo reboot"
fi
echo "  Group changes apply to app-zozobox at its next start: sudo systemctl restart app-zozobox (once it is installed)"
echo "  Next (docs/DEPLOY.md): fill in $CONF/env, then git-deploy install daily-panel"
