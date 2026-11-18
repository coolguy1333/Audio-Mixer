**Linux:** download `audio-mixer-1.2.0-linux.tar.gz`, unpack it and run `./install.sh` (installs for your user, no root). Arch users can also use the AUR package `audio-mixer-git`.
**Windows:** download `AudioMixerSoundboard.exe` and run it (no install). Windows may warn that it's unsigned: choose **More info ▸ Run anyway**. The Windows app is the soundboard; the full mixer needs PipeWire and is Linux-only.

## New in 1.2.0

### Linux mixer
- **Games go to the Games channel automatically.** Games from Steam, Proton, Wine, Lutris, Heroic and other launchers play through Games without being added to its app list. The Steam client, Discord, OBS and SteamVR are never treated as games.
- Each channel has an **Auto-add games** switch on its Apps tab; after updating it's on for the channel named Games. Games it picks up show there as "Detected as a game" while they play.
- Apps you assign to a channel by hand still go where you put them, so you can always correct a misdetection. Any Windows program run through Wine counts as a game.
- Apps that talk to PipeWire directly are now recognised by their program name too, so app rules match them more reliably.

### Windows soundboard
- No changes since 1.1.0; the same `.exe` is included so both downloads are in one place.

Setup guides: [Linux](https://github.com/coolguy1333/Audio-Mixer#install-any-distro) · [Windows](https://github.com/coolguy1333/Audio-Mixer/blob/main/windows/README.md)
