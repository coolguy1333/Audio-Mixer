#!/bin/sh
# Build dist/audio-mixer-<version>-linux.tar.gz from this checkout: the app, install.sh and uninstall.sh.
set -eu
cd "$(dirname "$0")/../.."
v=$(tr -d ' \n' < VERSION)
name="audio-mixer-$v-linux"
out="dist/$name"
rm -rf "$out" "dist/$name.tar.gz"
mkdir -p "$out/fx" "$out/packaging"
cp audio_mixer.py amx_effects.py amx_extras.py amx_sounds.py README.md LICENSE "$out/"
cp fx/amx_fx.c "$out/fx/"
cp -r web assets opendeck "$out/"
cp packaging/io.github.lawsonf.AudioMixer.desktop "$out/packaging/"
cp packaging/linux/install.sh packaging/linux/uninstall.sh "$out/"
chmod 755 "$out/install.sh" "$out/uninstall.sh" "$out/audio_mixer.py"
find "$out" -name __pycache__ -prune -exec rm -rf {} +
tar -C dist -czf "dist/$name.tar.gz" "$name"
echo "dist/$name.tar.gz"
