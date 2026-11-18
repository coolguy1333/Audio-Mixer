**Linux:** download `audio-mixer-1.1.0-linux.tar.gz`, unpack it and run `./install.sh` (installs for your user, no root). Arch users can also use the AUR package `audio-mixer-git`.
**Windows:** download `AudioMixerSoundboard.exe` and run it (no install). Windows may warn that it's unsigned: choose **More info ▸ Run anyway**. The Windows app is the soundboard; the full mixer needs PipeWire and is Linux-only.

## New in 1.1.0

### Linux mixer
- **Soundboard pop-out:** just the pads in a small window that can be pinned on top, locked, and reopened from the tray. Click a pad to play it, right-click to stop it.
- **Esc stops every sound** on the Soundboard page and in the pop-out.
- **Find sounds online** (opt-in): MyInstants, Freesound (free API key), Openverse, Wikimedia Commons, or paste a link (videos too, with yt-dlp).
- **The same sound can't be added twice**, and you can drag audio files onto the Soundboard page.
- **Fixed:** startup crashes with libadwaita 1.5 (Ubuntu 24.04) and older GLib, and a crash after editing a deck key.
- **New:** an install script for any distro (this download).

### Windows soundboard
- Pads with waveforms, trim, volume, colours and hotkeys that work while a game has focus.
- Plays into any device: with the free [VB-Cable](https://vb-audio.com/Cable/) your sounds go into Discord, OBS or games. A monitor output plays them on your headphones, and mic passthrough sends your voice along.
- Runs in the system tray, with an option to start with Windows; Stream Deck / script links.
- Same online sound search, duplicate check and zip backup (it can import the Linux soundboard's export).

Setup guides: [Linux](https://github.com/coolguy1333/Audio-Mixer#install-any-distro) · [Windows](https://github.com/coolguy1333/Audio-Mixer/blob/main/windows/README.md)
