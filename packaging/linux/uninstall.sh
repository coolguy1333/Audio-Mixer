#!/bin/sh
# Remove what install.sh installed. Your settings and sounds in ~/.config/audio-mixer stay.
set -eu
data=${XDG_DATA_HOME:-$HOME/.local/share}
rm -rf "$data/audio-mixer"
rm -f "$HOME/.local/bin/audio-mixer" "$data/applications/io.github.lawsonf.AudioMixer.desktop" \
      "$data/icons/hicolor/scalable/apps/io.github.lawsonf.AudioMixer.svg"
echo "Audio Mixer removed. Settings and sounds are still in ${XDG_CONFIG_HOME:-$HOME/.config}/audio-mixer."
