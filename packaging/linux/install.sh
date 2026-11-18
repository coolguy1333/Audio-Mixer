#!/bin/sh
# Install Audio Mixer for the current user (no root needed):
#   ~/.local/share/audio-mixer   the app
#   ~/.local/bin/audio-mixer     the command (also: audio-mixer ctl …)
#   a menu entry and the icon
# Your settings in ~/.config/audio-mixer are left alone. Run ./uninstall.sh to remove it again.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
data=${XDG_DATA_HOME:-$HOME/.local/share}
dest="$data/audio-mixer"
bin="$HOME/.local/bin"

missing=""
command -v python3 >/dev/null 2>&1 || missing="$missing python3"
if command -v python3 >/dev/null 2>&1 && ! python3 - >/dev/null 2>&1 <<'PY'
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: F401
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 5)
PY
then
    missing="$missing PyGObject+GTK4+libadwaita(1.5+)"
fi
command -v pactl >/dev/null 2>&1 || missing="$missing pactl"
if [ -n "$missing" ]; then
    echo "Audio Mixer needs:$missing"
    echo
    echo "  Arch / CachyOS:  sudo pacman -S python-gobject gtk4 libadwaita python-cairo libpulse gcc rnnoise ffmpeg"
    echo "  Ubuntu 24.04+:   sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 pulseaudio-utils pipewire-pulse gcc ffmpeg"
    echo "  Fedora:          sudo dnf install python3-gobject gtk4 libadwaita pulseaudio-utils gcc ffmpeg-free"
    echo
    echo "Then run this script again."
    exit 1
fi

echo "Installing to $dest"
rm -rf "$dest"
mkdir -p "$dest/fx" "$bin" "$data/applications" "$data/icons/hicolor/scalable/apps"
cp "$here"/audio_mixer.py "$here"/amx_*.py "$dest/"
cp "$here/fx/amx_fx.c" "$dest/fx/"
cp -r "$here/web" "$here/assets" "$here/opendeck" "$dest/"
cp "$here/README.md" "$here/LICENSE" "$dest/" 2>/dev/null || true

# the effects plugin (the app also builds it on first start if this fails)
if command -v gcc >/dev/null 2>&1; then
    if gcc -O2 -fPIC -shared -o "$dest/fx/amx_fx.so" "$dest/fx/amx_fx.c" -lrnnoise -lm 2>/dev/null; then
        echo "Built the effects plugin"
    elif gcc -O2 -fPIC -shared -o "$dest/fx/amx_fx.so" "$dest/fx/amx_fx.c" -DAMX_NO_RNNOISE -lm; then
        echo "Built the effects plugin without RNNoise (install rnnoise for noise suppression)"
    fi
else
    echo "Note: install gcc to use the effects"
fi

cat > "$bin/audio-mixer" <<SH
#!/bin/sh
exec python3 "$dest/audio_mixer.py" "\$@"
SH
chmod 755 "$bin/audio-mixer"
sed "s|^Exec=.*|Exec=$bin/audio-mixer|" "$here/packaging/io.github.lawsonf.AudioMixer.desktop" \
    > "$data/applications/io.github.lawsonf.AudioMixer.desktop"
cp "$here/assets/io.github.lawsonf.AudioMixer.svg" "$data/icons/hicolor/scalable/apps/"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$data/applications" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$data/icons/hicolor" || true

echo
echo "Done. Start Audio Mixer from your app menu, or run: audio-mixer"
case ":$PATH:" in
    *":$bin:"*) ;;
    *) echo "($bin isn't on your PATH yet; log out and back in, or run $bin/audio-mixer)" ;;
esac
