#!/bin/sh
# Real X11 check of the AppImage inside a distro container (offscreen tests miss xcb crashes):
#   docker run --rm -v $PWD/dist:/d:ro -v $PWD/packaging/test-x11.sh:/t.sh:ro ubuntu:24.04 /t.sh
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null; then
  apt-get update -qq >/dev/null; apt-get install -y -qq xvfb libgl1 libegl1 libfontconfig1 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 libxcb-xkb1 libdbus-1-3 libkrb5-3 libgssapi-krb5-2 ca-certificates >/dev/null 2>&1
  apt-get install -y -qq libglib2.0-0t64 >/dev/null 2>&1 || apt-get install -y -qq libglib2.0-0 >/dev/null 2>&1
else
  dnf install -y -q xorg-x11-server-Xvfb mesa-libGL mesa-libEGL fontconfig libxkbcommon-x11 xcb-util-cursor xcb-util-wm xcb-util-image xcb-util-keysyms xcb-util-renderutil dbus-libs krb5-libs glib2 >/dev/null 2>&1
fi
Xvfb :9 -screen 0 1280x800x24 >/dev/null 2>&1 &
sleep 2
cd /tmp && DISPLAY=:9 QT_QPA_PLATFORM=xcb PYTHONFAULTHANDLER=1 QT_DEBUG_PLUGINS=${QT_DEBUG_PLUGINS:-0} APPIMAGE_EXTRACT_AND_RUN=1 timeout 60 /d/PhoneCam-x86_64.AppImage --self-test 2>&1 | grep -vE "^ *File|Extension modules|^Current thread" | tail -${LINES_KEEP:-12}
echo "exit=$?"
