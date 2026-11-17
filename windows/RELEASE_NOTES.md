Audio Mixer Soundboard for Windows 10/11.

**New in 1.0.1**
- Fixed "Error opening OutputStream: Incompatible host API specific stream info [PaErrorCode -9984]" with *System default*, and the same problem with *System default microphone*. If WASAPI won't open a device, the soundboard now falls back to MME.
- **System tray:** closing the window keeps the soundboard running in the tray, so hotkeys and Stream Deck links keep working. Click the icon to show it again; right-click it to stop all sounds or quit. Opening the app again brings the window back.
- **Start with Windows** (⚙ ▸ Background) starts it hidden in the tray at login.
- The Stream Deck / script key now stays the same from the first run.

**Download `AudioMixerSoundboard.exe` below and run it.** There's nothing to install. Windows may warn that the file is unsigned: choose **More info ▸ Run anyway**.

- Pads with waveforms, trim, volume, colours, and drag to reorder
- Plays on any output device; with the free [VB-Cable](https://vb-audio.com/Cable/), sounds go into Discord, OBS or games
- Monitor on your headphones, and mic passthrough so friends hear you and the sounds
- Global hotkeys, and Stream Deck / script links
- Find sounds online (opt-in): MyInstants, Freesound, Openverse, Wikimedia Commons, or a link
- Drag and drop, no duplicate sounds, zip backup (also imports the Linux mixer's soundboard)

Setup guide: [windows/README.md](https://github.com/coolguy1333/Audio-Mixer/blob/main/windows/README.md)

Optional: `winget install Gyan.FFmpeg` (more formats) and `winget install yt-dlp.yt-dlp` (audio from video links).

This first release was tested on a Windows build machine without a sound card, and its audio on Linux. Please report problems with real devices (VB-Cable, hotkeys) as issues.
