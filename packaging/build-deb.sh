#!/bin/sh
set -eu
umask 022

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
version=$(sed -n "s/^VERSION = '\([0-9][0-9.]*\)'$/\1/p" "$source_dir/feed.py")
test -n "$version"
output=${1:-"./caesarsync_${version}_all.deb"}
staging=$(mktemp -d)
trap 'rm -rf -- "$staging"' EXIT
chmod 755 "$staging"

install -d "$staging/DEBIAN" "$staging/usr/bin" "$staging/usr/lib/caesarsync/icons" \
    "$staging/usr/share/applications" "$staging/usr/share/icons/hicolor/scalable/apps" \
    "$staging/usr/share/doc/caesarsync"
cat > "$staging/DEBIAN/control" <<EOF
Package: caesarsync
Version: $version
Section: net
Priority: optional
Architecture: all
Maintainer: mimmolimmo <23194675+mimmolimmo@users.noreply.github.com>
Depends: python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, gir1.2-secret-1, gir1.2-webkit2-4.1, lsof
Recommends: gnome-keyring
Homepage: https://github.com/mimmolimmo/caesarsync
Description: Sync selected Swisscom myCloud folders on Linux
 CaesarSync is an independent desktop client that keeps selected files in
 ordinary local folders and shows sync activity in the system tray.
 .
 It uses GTK and AppIndicator for the tray, WebKitGTK for Swisscom sign-in,
 and Secret Service to save WebDAV credentials and the sign-in session.
EOF

for source in app.py sync.py watcher.py feed.py signin.py; do
    install -m 644 "$source_dir/$source" "$staging/usr/lib/caesarsync/"
done
install -m 644 "$source_dir"/icons/cloud-*.svg "$staging/usr/lib/caesarsync/icons/"
install -m 644 "$source_dir/icons/cloud-ok.svg" \
    "$staging/usr/share/icons/hicolor/scalable/apps/ch.caesarlinx.CaesarSync.svg"
install -m 644 "$source_dir/LICENSE" "$staging/usr/share/doc/caesarsync/copyright"
install -m 644 "$source_dir/README.md" "$staging/usr/share/doc/caesarsync/README.md"

cat > "$staging/usr/bin/caesarsync" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/caesarsync/app.py "$@"
EOF
chmod 755 "$staging/usr/bin/caesarsync"

cat > "$staging/usr/share/applications/ch.caesarlinx.CaesarSync.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=CaesarSync
Comment=Sync selected Swisscom myCloud folders
Exec=caesarsync
Icon=ch.caesarlinx.CaesarSync
Terminal=false
Categories=Network;FileTransfer;
EOF

dpkg-deb --root-owner-group --build "$staging" "$output"
