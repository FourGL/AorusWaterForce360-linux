#!/bin/sh
# Installs the daemon, the control panel, its root helper and polkit policy. Run: sudo ui/install.sh
set -eu
cd "$(dirname "$0")/.."
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
[ -x AorusWaterForce360-linux ] || { echo "build first: go build -o AorusWaterForce360-linux main.go" >&2; exit 1; }

UNIT=AorusWaterForce360-linux
first_install=true
[ -f /etc/systemd/system/$UNIT.service ] && first_install=false

# display daemon
install -m 755 AorusWaterForce360-linux /usr/local/bin/AorusWaterForce360-linux
install -m 644 AorusWaterForce360-linux.service /etc/systemd/system/$UNIT.service

# control panel: root helper + polkit policy, the GTK app, launcher and icon
install -m 755 ui/awf360-ctl /usr/local/bin/awf360-ctl
install -m 644 ui/com.github.fourgl.awf360.policy /usr/share/polkit-1/actions/com.github.fourgl.awf360.policy
install -m 755 ui/awf360-ui.py /usr/local/bin/awf360-ui
install -D -m 644 ui/com.github.fourgl.awf360.desktop /usr/local/share/applications/com.github.fourgl.awf360.desktop
install -D -m 644 ui/com.github.fourgl.awf360.svg /usr/local/share/icons/hicolor/scalable/apps/com.github.fourgl.awf360.svg
# top bar temperature: GNOME Shell extension, turned on from the control panel
EXT=awf360-temp@fourgl.github.com
install -d /usr/local/share/gnome-shell/extensions/$EXT
install -m 644 ui/gnome-extension/$EXT/* /usr/local/share/gnome-shell/extensions/$EXT/
gtk-update-icon-cache -q -t /usr/local/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/local/share/applications 2>/dev/null || true

systemctl daemon-reload
if $first_install; then
	systemctl enable --now $UNIT
else
	systemctl try-restart $UNIT
fi
echo "done — open \"WaterForce 360\" from the app menu"
