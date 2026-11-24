#!/bin/sh
set -eu
cd "$(dirname "$0")"

exec /usr/bin/python3 - <<'PY'
import os
from pathlib import Path
from shutil import copy2

import gi

for namespace, version in (
    ('Gtk', '3.0'), ('AyatanaAppIndicator3', '0.1'), ('Secret', '1'),
):
    gi.require_version(namespace, version)

data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
app = data / 'caesarsync'
(app / 'icons').mkdir(parents=True, exist_ok=True)
for source in [Path('app.py'), Path('sync.py'), Path('launch'), *Path('icons').glob('cloud-*.svg')]:
    copy2(source, app / source)

def desktop_value(value):
    return value.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')

command = str(app / 'launch').replace('%', '%%')
for char in ('\\', '"', '`', '$'):
    command = command.replace(char, '\\' + char)
command = desktop_value('"' + command + '"')
icon = desktop_value(str(app / 'icons/cloud-ok.svg'))
launcher = data / 'applications' / 'ch.caesarlinx.CaesarSync.desktop'
launcher.parent.mkdir(parents=True, exist_ok=True)
launcher.write_text(f"""[Desktop Entry]
Type=Application
Name=CaesarSync
Comment=Sync selected Swisscom myCloud folders
Exec={command}
Icon={icon}
Terminal=false
Categories=Network;FileTransfer;
""", encoding='utf-8')
print('Installed CaesarSync. Open it from your application menu.')
PY
