#!/bin/bash
# Install klipper-power-loss-recovery next to an existing Klipper.
# Safe to run again (e.g. from Moonraker's update manager).
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"
KLIPPER="${KLIPPER_PATH:-$HOME/klipper}"
CONFIG="${CONFIG_PATH:-$HOME/printer_data/config}"
EXTRAS="$KLIPPER/klippy/extras"

[ -d "$EXTRAS" ] || { echo "Klipper not found at $KLIPPER (set KLIPPER_PATH)"; exit 1; }

for f in lost_z.py lost_z_make_resume.py; do
    ln -sf "$REPO/klippy/extras/$f" "$EXTRAS/$f"
    echo "linked $EXTRAS/$f"
    # Keep Klipper's own repo from reporting "dirty" because of these files.
    grep -qxF "klippy/extras/$f" "$KLIPPER/.git/info/exclude" 2>/dev/null \
        || echo "klippy/extras/$f" >> "$KLIPPER/.git/info/exclude"
done

if [ -d "$CONFIG" ] && [ ! -e "$CONFIG/power_loss.cfg" ]; then
    cp "$REPO/config/power_loss.cfg" "$CONFIG/power_loss.cfg"
    echo "copied power_loss.cfg to $CONFIG"
fi

cat <<'EOF'

Done. If this is a first install, add to printer.cfg:

  [include power_loss.cfg]

  [lost_z]
  lift: 5

then restart the Klipper service:  sudo systemctl restart klipper
EOF