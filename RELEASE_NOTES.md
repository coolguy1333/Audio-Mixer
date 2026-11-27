**Linux:** download `audio-mixer-1.3.0-linux.tar.gz`, unpack it and run `./install.sh` (installs for your user, no root). Arch users can also use the AUR package `audio-mixer-git`.
**Windows:** download `AudioMixerSoundboard.exe` and run it (no install). Windows may warn that it's unsigned: choose **More info ▸ Run anyway**. The Windows app is the soundboard; the full mixer needs PipeWire and is Linux-only.

## New in 1.3.0

### Windows soundboard
- **Pop-out window** with just small pads (name and waveform). Click a pad to play it, right-click to stop it, and **Esc** stops every sound. It has a slim bar with stop all, 📌 always on top, and a button to bring back the main window. It remembers its size and position, reopens at start if it was open, and the tray menu can show or hide it.
- **The main window shrinks much further** (down to 200×150). When it's small it switches to compact pads; right-click a pad to edit it.
- **Display scaling fixes:** at 125–200% scaling the pop-out no longer grows each time it reopens, and it can be made small.

### Linux mixer
- **The Soundboard page stays where you were** after you edit, rename or delete a sound, instead of jumping back to the top. Same in the pop-out.

Setup guides: [Linux](https://github.com/coolguy1333/Audio-Mixer#install-any-distro) · [Windows](https://github.com/coolguy1333/Audio-Mixer/blob/main/windows/README.md)
