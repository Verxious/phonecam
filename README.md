# PhoneCam

Native Linux application that turns a phone camera into a webcam for Discord, OBS and other V4L2 applications. Python/PySide6 interface, scrcpy for Android camera capture, FFmpeg for processing and preview, v4l2loopback for the virtual camera. Local processing; no cloud service.

## Install

**AppImage (any modern x86-64 Linux):** download `PhoneCam-x86_64.AppImage` from [Releases](https://github.com/Verxious/phonecam/releases/latest), make it executable (file properties → *Allow executing*, or `chmod +x`) and open it. Python, Qt, OpenCV, FFmpeg, scrcpy and adb are inside. The only thing it cannot carry is the kernel driver for the virtual camera (v4l2loopback); on first connect PhoneCam offers to install it with your distribution's package manager (asks for the admin password once). The AppImage adds itself to the application menu and shows **Νέα έκδοση** when a newer release exists; one click downloads it and restarts.

Works on older processors too: the AppImage uses Qt 6.9 and numpy 2.3, because newer wheels require SSE4.2 and POPCNT (x86-64-v2) and stop with *“This Qt build requires the following features: sse4.2 popcnt”*. Tested under emulation of an Intel Core 2 (Penryn).

**From source:** `git clone https://github.com/Verxious/phonecam && phonecam/install.sh`. The installer checks every dependency and installs what is missing (Arch, Debian/Ubuntu, Fedora). A source checkout updates itself: new commits are fetched in the background and applied on the next start.

## Run

```sh
./run.sh
./run.sh --mode usb --autostart
./run.sh --mode wifi --autostart
python install_launchers.py
```

The **PhoneCam** desktop launcher starts the application and reconnects the last source. Only one instance runs at a time. Existing POCO launchers also open PhoneCam.

Dependencies: Python 3.11+, PySide6, scrcpy with camera support (2.2+), adb, FFmpeg with its zmq filter enabled, the system libzmq library, v4l2loopback and v4l2loopback-ctl. On Arch/CachyOS these are supplied by `python-pyside6`, `scrcpy`, `android-tools`, `ffmpeg`, `v4l2loopback-dkms`, and `v4l2loopback-utils`, with kernel headers matching the running kernel. On another distribution install its corresponding packages. A missing virtual camera triggers a normal system administrator authentication dialog; existing OBS virtual cameras are preserved.

## Android 12 and newer

1. Enable Developer options and USB debugging on the phone.
2. Connect a USB data cable and accept the debugging permission on the phone.
3. Select **Android 12+**, the phone, and **USB** or **Wi-Fi**. Click **Σύνδεση**.
4. In Discord or OBS select the camera name shown in PhoneCam (currently **POCO Webcam** on this PC).

No camera application needs to be installed on the phone. Wi-Fi setup initially needs USB and both devices on the same local network. Unplug USB after a working Wi-Fi picture appears. Restarting the phone or changing its IP may require USB setup again. A USB connection powers the phone; charging speed depends on the PC port and cable, and may be slower than the phone charger.

The app discovers the actual camera IDs and supported sizes. Start with 1080p / 30 FPS. 60 FPS is experimental when the camera does not advertise it. The live badge measures decoded frames; requesting 60 cannot make a 30 FPS source produce 60 different frames. Stop Discord video before changing output dimensions. If a new profile fails, the last working profile is restored.

## Android 10 (and older compatible phones)

scrcpy's direct camera capture requires Android 12+. For Android 10 use a phone application that provides an HTTP/MJPEG or RTSP video stream, for example [IP Webcam](https://play.google.com/store/apps/details?id=com.pas.webcam).

1. Install the phone camera app and grant its camera permission.
2. Start its camera server. For IP Webcam this is **Start server**; its video URL is typically `http://PHONE_IP:8080/video`, rather than just the server's web page.
3. In PhoneCam select **Android 10 / iPhone · HTTP / RTSP** and paste the video URL.
4. For Wi-Fi leave **Μέσω USB / ADB** off; use the same local network.
5. For USB enable USB debugging, connect a data cable, approve the phone, tick **Μέσω USB / ADB**, and select the USB phone. Keep the phone app server running. PhoneCam forwards the URL's TCP port over ADB automatically. For IP Webcam an example URL is `http://127.0.0.1:8080/video`; PhoneCam replaces its host with the allocated local port. Wi-Fi is not required if the phone application keeps serving over localhost without it.

Quality and frame rate also depend on the phone application's own settings. HTTP/MJPEG may use substantially more bandwidth than H.264/RTSP. TCP RTSP is used for USB forwarding. Applications that require UDP-only video are incompatible with this USB path. Some applications require Wi-Fi to start their server; that is a phone application limitation.

## iPhone

Use an iPhone application that actually publishes an accessible HTTP/MJPEG or RTSP stream and supply its video URL over the local network. No native iPhone USB backend is implemented. iPhone hardware has not been tested. Android 10 hardware has not been tested; the HTTP capture path is tested using a local stream.

## Image controls

Rotation, horizontal Mirror, **Φυσικό**, **Ζεστό**, **Ασπρόμαυρο**, **Έντονα χρώματα**, and brightness affect the virtual camera as well as the preview. These controls update active filters without restarting capture or closing the virtual camera. Rotation keeps the selected output dimensions and fits the full image inside them, adding black margins where needed. Resolution, FPS, bitrate, source and physical camera changes restart capture. **Reset** restores natural color, brightness, rotation and Mirror; it preserves your quality selection.

## Background

**Φόντο** replaces the room behind you with an image or a video; you stay in front. Person matting runs on the CPU every frame (bundled MediaPipe selfie segmenter, Apache 2.0) and costs about two extra cores at 1080p.

- **Image + Κινούμενα εφέ**: the image is analysed once (SegFormer-B2 ADE20K scene model, ~55 MB, downloaded on first use to `~/.config/phonecam/models`, NVIDIA research licence) and a 12 s seamless loop is rendered: drifting clouds, rolling waves, horizon mist and flickering lamps. Land, ships and people in the image stay still. The still image is shown while it renders.
- **Video + Seamless loop**: the video is converted once to the camera size and rate, and its last 1.5 s dissolve into its beginning so the repeat is invisible.
- **YouTube link**: paste a link (YouTube or any site yt-dlp supports); the video is downloaded once (up to 1080p, no audio) and looped like a file. yt-dlp is fetched automatically if it is not installed.
- **Mirror φόντου** flips only the background; **Mirror** flips only you.
- **Ποιότητα loops** (in the background manager): *Υψηλή*, *Κανονική* (about half the size) or *Μικρή* (about a quarter). Changing it never re-renders existing loops unless you ask.
- **Κάδρο** decides how a background with another shape fits: *Αυτόματο* fills the screen when shapes are close and shows portrait videos whole; *Γέμισμα οθόνης* crops; *μαύρες* or *θολές μπάρες* show everything.
- **Διαχείριση · προσθήκη φόντων…** lists every background with a thumbnail. Removing one deletes what PhoneCam made for it (rendered loops, downloaded videos) but never your own files; **Καθαρισμός cache** frees all loops except the active one.

Loops are cached in `~/.config/phonecam/backgrounds` per resolution and FPS. Rendering uses at most three low-priority processes.

If Discord displays an error about setting the video background, select **Video Background → None**, then reopen video.

## Soundboard

The **🎛 Soundboard** tab sits next to **📷 Κάμερα**; both keep running while you switch. Sounds are colour pads that light up and show their progress while playing; loops (🔁) keep going behind your voice until stopped. Keys **1–0, Q–P, A–L, Z–M** play the pads by key position, so they work with a Greek keyboard layout too; **Esc** or **■ STOP** stops everything. **Master ένταση** scales all pads.

Add a pad from a link (YouTube or any site yt-dlp supports; only the audio is downloaded) or from a file. In its editor drag across the **waveform** to choose what plays (drag the edges to adjust, or type exact seconds), set volume, repeat and pad colour, and **▶ Δοκιμή** on your own speakers. Right click a pad to edit, move or remove it.

**ON AIR** creates a virtual microphone, «PhoneCam Mic», carrying your voice (optional) plus the pads; select it as the input device in Discord. **Ακούω κι εγώ τους ήχους** also plays them on your speakers. Uses PulseAudio or PipeWire through `pactl`/`paplay` (pulseaudio-utils, or libpulse on Arch). The microphone is removed when PhoneCam closes and comes back the next time it starts. Turn off Discord's noise suppression for music, otherwise it filters the sounds out.

Removing a pad deletes its downloaded audio; your own files are never touched.

## Development

```sh
python -m unittest discover -s tests -v
```

Modules: `app` interface, `sound` virtual microphone and playback, `soundboard` sound library, `library` background manager, `youtube` link downloads, `updater` updates, `driver` v4l2loopback setup, `scene` background analysis and loops, `compositor` person matting, `weights` model files, `android` discovery and connection, `network` USB forwarding, `engine` video pipeline, `live` runtime filter control, `virtual` webcam device, `models` validation and metadata, `config` atomic preferences. Configuration is stored in `~/.config/phonecam`; legacy POCO settings are imported on first run. Stream URLs may contain passwords, so the preferences file is private (mode 0600).

Upstream camera documentation: [scrcpy camera capture](https://github.com/Genymobile/scrcpy/blob/master/doc/camera.md).
