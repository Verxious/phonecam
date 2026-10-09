"""Install per-user desktop launchers; no system changes or dependencies."""
from pathlib import Path
import os

root = Path(__file__).resolve().parent
home = Path.home()
applications = Path(os.environ.get('XDG_DATA_HOME', str(home / '.local/share'))) / 'applications'
applications.mkdir(parents=True, exist_ok=True)
desktop = home / 'Desktop'
icon = root / 'phonecam/assets/phonecam.svg'


def entry(name, options='', comment='Το κινητό σου ως webcam · USB / Wi-Fi / HTTP / RTSP'):
    command = str(root / 'run.sh').replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$')
    return f'''[Desktop Entry]
Type=Application
Name={name}
Comment={comment}
Exec="{command}" {options}
Icon={icon}
Terminal=false
Categories=AudioVideo;Video;
StartupNotify=false
'''


launchers = {'phonecam.desktop': entry('PhoneCam', '--autostart')}
# Upgrade the previous project launchers when they are present.
for filename, name, options in (
    ('poco-webcam-usb.desktop', 'PhoneCam USB', '--mode usb --autostart'),
    ('poco-webcam-wifi.desktop', 'PhoneCam Wi-Fi', '--mode wifi --autostart'),
    ('poco-webcam-image.desktop', 'PhoneCam — Εικόνα & Φίλτρα', ''),
):
    if (applications / filename).exists():
        launchers[filename] = entry(name, options)
for filename, content in launchers.items():
    (applications / filename).write_text(content)
    if desktop.is_dir() and (filename == 'phonecam.desktop' or (desktop / filename).exists()):
        path = desktop / filename
        path.write_text(content)
        path.chmod(0o755)
print('Installed PhoneCam launchers in', applications)
