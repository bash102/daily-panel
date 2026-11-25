#!/usr/bin/env bash
# Install or update Zozo's box on a Raspberry Pi (Raspberry Pi OS Bookworm Lite).
#
#   sudo git clone <repo> /opt/zozobox/src     # or copy the repo anywhere
#   sudo /opt/zozobox/src/deploy/install.sh    # run as root; safe to re-run
#
# Options:
#   --no-apt      skip apt-get
#   --no-pip      skip pip (the venv keeps what it has; the package is an
#                 editable install, so code changes still take effect, but
#                 new dependencies or console scripts need a pip run)
#   --offline     --no-apt --no-pip: a re-run with no network at all
#   --no-enable   install files but do not enable the units
#
# Idempotent. It never overwrites /etc/zozobox/box.toml,
# /etc/zozobox/secrets.env or an existing /etc/asound.conf (a changed
# template is written next to it as *.zozobox-new). Running services are
# restarted (try-restart) so an update takes effect; nothing is started on a
# fresh install until you have filled in the secrets (docs/DEPLOY.md).
set -euo pipefail

PREFIX=/opt/zozobox
SRC=$PREFIX/src
VENV=$PREFIX/venv
DATA=/var/lib/zozobox
ETC=/etc/zozobox
USER_NAME=zozobox
GROUPS_WANTED=(gpio i2c audio lp plugdev)
UNITS=(zozobox-engine.service zozobox-dashboard.service zozobox-sync.service
       zozobox-bootstrap.service zozobox-nightly.service zozobox-nightly.timer
       zozobox-mood-reprocess.service zozobox-mood-reprocess.timer)
ENABLE=(zozobox-engine.service zozobox-dashboard.service zozobox-sync.service
        zozobox-nightly.timer zozobox-mood-reprocess.timer)
APT_PACKAGES=(python3-venv python3-dev alsa-utils ffmpeg libusb-1.0-0 fonts-dejavu-core
              i2c-tools git
              # gpiozero's lgpio pin factory (Pi OS packages; the venv sees them)
              python3-lgpio)

DO_APT=1
DO_PIP=1
DO_ENABLE=1
for a in "$@"; do
  case "$a" in
    --no-apt) DO_APT=0 ;;
    --no-pip) DO_PIP=0 ;;
    --offline) DO_APT=0; DO_PIP=0 ;;
    --no-enable) DO_ENABLE=0 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }

[[ $EUID -eq 0 ]] || { echo "run as root (sudo $0)" >&2; exit 1; }
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEPLOY=$REPO/deploy
[[ -f $REPO/pyproject.toml && -d $REPO/zozobox ]] || { echo "not a zozobox checkout: $REPO" >&2; exit 1; }

# -- packages -----------------------------------------------------------------
if [[ $DO_APT -eq 1 ]]; then
  say "apt packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  # python3-lgpio exists on Raspberry Pi OS only; don't fail elsewhere.
  if ! apt-get install -y -q "${APT_PACKAGES[@]}"; then
    warn "python3-lgpio not available; installing the rest (pip builds lgpio)"
    rest=()
    for p in "${APT_PACKAGES[@]}"; do [[ $p == python3-lgpio ]] || rest+=("$p"); done
    apt-get install -y -q "${rest[@]}" swig liblgpio-dev || apt-get install -y -q "${rest[@]}"
  fi
fi

# -- I2C (PCA9685 LED driver) -----------------------------------------------------
if command -v raspi-config >/dev/null 2>&1; then
  say "enable I2C"
  raspi-config nonint do_i2c 0 || warn "raspi-config could not enable I2C"
else
  warn "raspi-config not found; enable I2C yourself (dtparam=i2c_arm=on)"
fi

# -- user and groups ------------------------------------------------------------
say "user $USER_NAME"
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$DATA" --no-create-home \
          --shell /usr/sbin/nologin "$USER_NAME"
fi
for g in "${GROUPS_WANTED[@]}"; do
  getent group "$g" >/dev/null || groupadd --system "$g"
  usermod -aG "$g" "$USER_NAME"
done

# -- directories ------------------------------------------------------------------
say "directories"
install -d -m 0755 "$PREFIX" "$ETC"
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" "$DATA"
if [[ -d $DATA ]] && ! mountpoint -q "$DATA"; then
  warn "$DATA is not a separate mount. Fine for testing; for the read-only root put it on the persistent data partition (docs/DEPLOY.md)."
fi

# -- source -------------------------------------------------------------------------
if [[ $REPO != "$SRC" ]]; then
  say "copy source to $SRC"
  # Into a fresh directory, then swap: files removed upstream don't linger.
  new=$(mktemp -d "$PREFIX/.src-new.XXXXXX")
  tar -C "$REPO" --exclude=./.git --exclude=./cloud/worker/node_modules \
      --exclude='__pycache__' --exclude='*.pyc' -cf - . | tar -C "$new" -xf -
  chmod 0755 "$new"
  rm -rf "$PREFIX/.src-old"
  [[ -e $SRC ]] && mv "$SRC" "$PREFIX/.src-old"
  mv "$new" "$SRC"
  rm -rf "$PREFIX/.src-old"
fi
chmod -R a+rX "$SRC"

# -- virtualenv ---------------------------------------------------------------------
say "virtualenv $VENV"
if [[ ! -x $VENV/bin/python ]]; then
  [[ $DO_PIP -eq 1 ]] || { echo "no venv at $VENV yet: run once without --no-pip/--offline" >&2; exit 1; }
  # System site packages: picks up python3-lgpio from apt.
  python3 -m venv --system-site-packages "$VENV"
fi
if [[ $DO_PIP -eq 1 ]]; then
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -e "$SRC[pi]"
else
  echo "  skipped pip (--no-pip)"
fi
# The services cannot write to $SRC, so compile once here; otherwise every
# start (and every timer run) recompiles all modules on the Pi 3's CPU.
"$VENV/bin/python" -m compileall -q "$SRC/zozobox" >/dev/null
chmod -R a+rX "$SRC/zozobox"

say "chimes"
runuser -u "$USER_NAME" -- "$VENV/bin/python" -m zozobox.assets "$DATA/chimes" >/dev/null
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" "$DATA/soundboard" "$DATA/assets" "$DATA/assets/phrases"

# -- config files (never overwritten) ----------------------------------------------------
say "config in $ETC"
install -m 0644 "$DEPLOY/box.toml.example" "$ETC/box.toml.example"
if [[ ! -e $ETC/box.toml ]]; then
  install -m 0644 "$DEPLOY/box.toml.example" "$ETC/box.toml"
  echo "  created $ETC/box.toml (edit pins, cards, ceilings during bring-up)"
else
  echo "  kept existing $ETC/box.toml"
fi
if [[ ! -e $ETC/secrets.env ]]; then
  install -m 0600 -o root -g root "$DEPLOY/secrets.env.example" "$ETC/secrets.env"
  echo "  created $ETC/secrets.env from the template: FILL IT IN"
else
  chmod 0600 "$ETC/secrets.env"; chown root:root "$ETC/secrets.env"
  echo "  kept existing $ETC/secrets.env"
fi

install_or_offer() {  # src dst mode: install when missing, else offer *.zozobox-new
  local src=$1 dst=$2 mode=$3
  if [[ ! -e $dst ]]; then
    install -m "$mode" "$src" "$dst"; echo "  installed $dst"
  elif ! cmp -s "$src" "$dst"; then
    install -m "$mode" "$src" "$dst.zozobox-new"
    echo "  kept $dst (template differs: see $dst.zozobox-new)"
  fi
}

say "ALSA (softvol hard caps)"
install_or_offer "$DEPLOY/asound.conf" /etc/asound.conf 0644

# -- udev: QL-800 ----------------------------------------------------------------------
say "udev rule for the QL-800"
install -m 0644 "$DEPLOY/udev/99-zozobox-ql800.rules" /etc/udev/rules.d/99-zozobox-ql800.rules
udevadm control --reload-rules || true
udevadm trigger --subsystem-match=usb --attr-match=idVendor=04f9 || true

# -- sudoers: only the poweroff ------------------------------------------------------------
say "sudoers drop-in (poweroff only)"
tmp=$(mktemp)
install -m 0440 "$DEPLOY/sudoers/zozobox-poweroff" "$tmp"
if visudo -cf "$tmp" >/dev/null; then
  install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/zozobox-poweroff
else
  warn "sudoers drop-in failed visudo; not installed (the parent chord cannot power off)"
fi
rm -f "$tmp"

# -- systemd --------------------------------------------------------------------------------
say "systemd units"
for u in "${UNITS[@]}"; do
  install -m 0644 "$DEPLOY/systemd/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
if [[ $DO_ENABLE -eq 1 ]]; then
  systemctl enable "${ENABLE[@]}" >/dev/null
  systemctl try-restart zozobox-engine.service zozobox-dashboard.service zozobox-sync.service || true
fi

PLACEHOLDER_RE='^[A-Z_]+=(replace-me|.*example\.workers\.dev|.*<)'
if grep -Eq "$PLACEHOLDER_RE" "$ETC/secrets.env"; then
  warn "$ETC/secrets.env still has placeholders:"
  grep -E "$PLACEHOLDER_RE" "$ETC/secrets.env" | cut -d= -f1 | sed 's/^/    /' >&2
  echo "  Fill it in, then:"
  echo "  sudo systemctl start zozobox-bootstrap   # phrases, evergreen, soundboard (one time)"
  echo "  sudo systemctl start ${ENABLE[*]}"
elif [[ $DO_ENABLE -eq 1 ]]; then
  # Timers enabled by this run (first install or a new one) start now, not
  # only after the next reboot. Services are left as they are (try-restart
  # above), but say which ones are not running.
  systemctl start zozobox-nightly.timer zozobox-mood-reprocess.timer || true
  for u in zozobox-engine.service zozobox-dashboard.service zozobox-sync.service; do
    systemctl is-active -q "$u" || echo "  $u is not running: sudo systemctl start $u"
  done
fi
if [[ ! -e $DATA/dashboard_auth.json ]]; then
  echo "  No dashboard password yet. Once zozobox-dashboard runs:"
  echo "    sudo cat $DATA/dashboard-setup-token"
  echo "  then open http://<this box>:8080/setup?token=<that token> and set it."
fi
say "done. Next: docs/DEPLOY.md and docs/BRINGUP.md"
