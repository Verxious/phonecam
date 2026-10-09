#!/usr/bin/env bash
# Build PhoneCam-x86_64.AppImage: Python, Qt, OpenCV, FFmpeg, scrcpy and adb inside.
# Only the v4l2loopback kernel driver stays on the host (the app offers to install it).
#   packaging/build-appimage.sh [owner/repo for updates]
# Needs: curl, tar, docker (for old-glibc X11 helper libraries).
# Qt < 6.10 and numpy < 2.4: newer wheels need x86-64-v2 (SSE4.2, POPCNT) and would
# refuse to start on older processors ("This Qt build requires the following features").
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
ROOT=$PWD
REPOSITORY=${1:-${PHONECAM_REPOSITORY:-}}
VERSION=$(python3 -c 'import re;print(re.search(r"\d+\.\d+\.\d+", open("phonecam/__init__.py").read())[0])')
WORK=${WORK:-$ROOT/build/appimage}
CACHE=$WORK/downloads
APPDIR=$WORK/AppDir
OUT=$ROOT/dist/PhoneCam-x86_64.AppImage
PYTHON_URL=https://github.com/niess/python-appimage/releases/download/python3.12/python3.12.15-cp312-cp312-manylinux2014_x86_64.AppImage
FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz
TOOL_URL=https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage

fetch() { [ -s "$CACHE/$2" ] || curl -fL --retry 3 -o "$CACHE/$2" "$1"; }
mkdir -p "$CACHE" "$ROOT/dist"
rm -rf "$APPDIR"

echo '== Python'
fetch "$PYTHON_URL" python.AppImage
chmod +x "$CACHE/python.AppImage"
(cd "$WORK" && rm -rf squashfs-root && "$CACHE/python.AppImage" --appimage-extract >/dev/null && mv squashfs-root "$APPDIR")
PY=$APPDIR/opt/python3.12/bin/python3.12
"$PY" -m pip install --quiet --no-warn-script-location --upgrade pip
"$PY" -m pip install --quiet --no-warn-script-location 'PySide6-Essentials>=6.6,<6.10' 'numpy>=1.26,<2.4' 'opencv-python-headless>=4.8' pyzmq certifi

echo '== Trim unused Qt (QML/Quick, tools, translations)'
QT=$("$PY" -c 'import PySide6,os;print(os.path.dirname(PySide6.__file__))')
rm -rf "$QT"/Qt/qml "$QT"/Qt/translations "$QT"/Qt/libexec "$QT"/designer "$QT"/assistant "$QT"/linguist "$QT"/lupdate "$QT"/lrelease "$QT"/qmllint "$QT"/qmlformat "$QT"/qmlls "$QT"/typesystems "$QT"/include "$QT"/glue
for module in Qml Quick Designer Pdf Sql Test Help UiTools Concurrent OpenGLWidgets Labs ShaderTools; do
    rm -f "$QT"/Qt/lib/libQt6"$module"*.so* "$QT"/Qt"$module"*.so "$QT"/Qt"$module"*.pyi
done
rm -rf "$QT"/Qt/plugins/{sqldrivers,qmltooling,designer,generic,networkinformation,tls/libqopensslbackend.so.disabled}
find "$APPDIR/opt/python3.12/lib/python3.12" -name tests -prune -type d -path '*numpy*' -exec rm -rf {} +
rm -rf "$APPDIR/opt/python3.12/lib/python3.12/"{idlelib,tkinter,turtledemo,ensurepip,lib2to3} "$APPDIR"/opt/python3.12/lib/python3.12/site-packages/pip

echo '== PhoneCam'
SITE=$("$PY" -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])')
rm -rf "$SITE/phonecam"
cp -r phonecam "$SITE/phonecam"
find "$SITE/phonecam" -name __pycache__ -prune -exec rm -rf {} +
printf "REPOSITORY = %s\nAPPIMAGE = True\n" "$(python3 -c 'import sys;print(repr(sys.argv[1]))' "$REPOSITORY")" > "$SITE/phonecam/release.py"
"$PY" -m compileall -q "$SITE/phonecam"

echo '== FFmpeg, scrcpy, adb'
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib" "$APPDIR/usr/share/scrcpy"
fetch "$FFMPEG_URL" ffmpeg.tar.xz
tar -xJf "$CACHE/ffmpeg.tar.xz" -C "$WORK" --wildcards '*/bin/ffmpeg'
cp "$WORK"/ffmpeg-*/bin/ffmpeg "$APPDIR/usr/bin/"
SCRCPY_URL=$(curl -fsSL https://api.github.com/repos/Genymobile/scrcpy/releases/latest | grep -o 'https://[^"]*scrcpy-linux-x86_64[^"]*\.tar\.gz' | head -1)
fetch "$SCRCPY_URL" scrcpy.tar.gz
tar -xzf "$CACHE/scrcpy.tar.gz" -C "$APPDIR/usr/share/scrcpy" --strip-components=1
ln -sf ../share/scrcpy/scrcpy "$APPDIR/usr/bin/scrcpy"
ln -sf ../share/scrcpy/adb "$APPDIR/usr/bin/adb"

echo '== X11 helper libraries (Qt xcb plugin) from an old glibc base'
docker run --rm -v "$APPDIR/usr/lib:/out" ubuntu:20.04 bash -c '
    apt-get update -qq >/dev/null && cd /tmp &&
    apt-get download -qq libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-util1 libxkbcommon-x11-0 libxcb-xkb1 libgssapi-krb5-2 libkrb5-3 libk5crypto3 libkrb5support0 libkeyutils1 >/dev/null &&
    for deb in *.deb; do dpkg-deb -x "$deb" root; done &&
    cp -a root/usr/lib/x86_64-linux-gnu/*.so.* /out/ && { cp -a root/lib/x86_64-linux-gnu/*.so.* /out/ 2>/dev/null || true; } && chown -R '"$(id -u):$(id -g)"' /out'

echo '== AppDir entry'
rm -f "$APPDIR"/*.desktop "$APPDIR"/*.png "$APPDIR"/*.svg "$APPDIR/.DirIcon" "$APPDIR/AppRun"
cp phonecam/assets/phonecam.svg "$APPDIR/phonecam.svg"
ln -sf phonecam.svg "$APPDIR/.DirIcon"
cat > "$APPDIR/phonecam.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=PhoneCam
Comment=Το κινητό σου ως webcam · USB / Wi-Fi / HTTP / RTSP
Exec=phonecam
Icon=phonecam
Terminal=false
Categories=AudioVideo;Video;
X-AppImage-Version=$VERSION
EOF
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE=$(dirname "$(readlink -f "$0")")
export PATH="$HERE/usr/bin:$HERE/opt/python3.12/bin:$PATH"
# Bundled X11 helpers only fill gaps; host graphics drivers keep priority.
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}$HERE/usr/lib"
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
# The bundled OpenSSL does not know where this distribution keeps its CA certificates.
if [ -z "${SSL_CERT_FILE:-}" ]; then
    for bundle in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/ca-bundle.pem /etc/ssl/cert.pem; do
        [ -s "$bundle" ] && export SSL_CERT_FILE="$bundle" && break
    done
    [ -n "${SSL_CERT_FILE:-}" ] || export SSL_CERT_FILE="$(echo "$HERE"/opt/python3.12/lib/python3.12/site-packages/certifi/cacert.pem)"
fi
unset PYTHONHOME PYTHONPATH
exec "$HERE/opt/python3.12/bin/python3.12" -s -m phonecam "$@"
EOF
chmod +x "$APPDIR/AppRun"

echo '== Pack'
fetch "$TOOL_URL" appimagetool.AppImage
chmod +x "$CACHE/appimagetool.AppImage"
rm -f "$OUT"
ARCH=x86_64 "$CACHE/appimagetool.AppImage" --appimage-extract-and-run --no-appstream "$APPDIR" "$OUT" >/dev/null
ls -lh "$OUT"
