# Audio Mixer Soundboard for Windows

A soundboard for Windows 10 and 11, built from the Linux Audio Mixer's soundboard. The full mixer (channels, mixes, per-app routing, effects) needs PipeWire and stays Linux-only.

- **Pads** with waveforms. Click to play; click again to layer another copy. Use ⋯ to trim on the waveform and set volume, colour and a hotkey. Drag pads to reorder them.
- **Plays on any output device.** Choose VB-Cable to reach Discord, OBS or a game, and add a **monitor** device (your headphones) so you hear the sounds yourself.
- **Mic passthrough** mixes your microphone into the same output, so the people you talk to hear your voice and the sounds on one virtual mic.
- **Global hotkeys** work while a game has focus. A sound's hotkey toggles it, and there's an optional Stop all hotkey.
- **Find sounds online** is opt-in. It searches MyInstants, Freesound (free API key), Openverse and Wikimedia Commons, or takes a link. With [yt-dlp](https://github.com/yt-dlp/yt-dlp) installed, a link can also be a video (YouTube, TikTok, …).
- **The same audio can't go on the board twice.**
- **Drag and drop** audio files onto the window.
- **Stream Deck and scripts:** every sound has a URL that plays, stops or toggles it.
- **Phone companion** (opt-in): use the board from a phone on your network.
- **Backup:** export and import all sounds as one `.zip`. It also imports the Linux mixer's soundboard exports.

## Install

1. Download `AudioMixerSoundboard.exe` from the [latest release](https://github.com/coolguy1333/Audio-Mixer/releases/latest).
2. Run it. It's a single file with nothing to install. Windows SmartScreen may warn that the file is unsigned: choose **More info ▸ Run anyway**.
3. The window needs Microsoft Edge WebView2, which Windows 10/11 already have. If it's missing, the soundboard opens in your web browser instead.

Optional extras:

```bat
winget install Gyan.FFmpeg
```

```bat
winget install yt-dlp.yt-dlp
```

Without ffmpeg, MP3, WAV, FLAC and OGG files work; ffmpeg adds M4A, AAC, Opus, WebM and WMA. yt-dlp, which also needs ffmpeg, lets a link take the audio of a video.

## Use it in Discord, OBS or a game

1. Install the free [VB-Cable](https://vb-audio.com/Cable/) and restart.
2. **Plays on** → *CABLE Input (VB-Audio Virtual Cable)*.
3. In Discord, open **Settings ▸ Voice & Video ▸ Input device** and choose *CABLE Output (VB-Audio Virtual Cable)*. Do the same in OBS or the game.
4. **Mic passthrough** → your microphone, so they hear you too.
5. **Also hear it on** → your headphones, so you hear the sounds yourself.

Discord's noise suppression (Krisp) can cut sound effects; turn it off if sounds get chopped.

## Hotkeys

Open a pad's ⋯ editor and click **Set hotkey**, then press the keys (for example Ctrl+Alt+1, or F13 to F24 on a macro keyboard). Hotkeys work while other apps have focus. They don't reach apps running as administrator unless the soundboard runs as administrator too.

## Stream Deck, AutoHotkey, scripts

**⚙ ▸ Stream Deck and scripts** shows a link like this:

```
http://127.0.0.1:8788/api/play/1234?t=YOUR_KEY
```

Swap `play` for `stop` or `toggle`; `/api/stopall?t=…` stops everything. Use the link with a Stream Deck web-request action (or the built-in "Website" action with "Access in background" ticked), AutoHotkey's `Download`, or `curl`. The key stops websites you visit from controlling the soundboard. If port 8788 is taken, the app says so and uses another port.

## Where things are kept

- Settings and sounds: `%APPDATA%\AudioMixerSoundboard` (`config.json`, `sounds\`, `log.txt`)
- Downloads: `%LOCALAPPDATA%\AudioMixerSoundboard\cache`

## System tray

The soundboard keeps running in the system tray when you close its window, so hotkeys, Stream Deck links and the phone companion keep working. Click the tray icon to bring the window back; right-click it to stop all sounds or quit. Starting the app again also brings the window back. In **⚙ ▸ Background** you can make closing the window quit instead, and turn on **Start with Windows**, which starts it hidden in the tray at login (`--tray`).

## Build it yourself

```bat
py -3.12 -m pip install -r windows\requirements.txt pyinstaller==6.14.1
```

```bat
pyinstaller --noconfirm --onefile --windowed --name AudioMixerSoundboard --icon windows/icon.ico --add-data "windows/ui;ui" --add-data "windows/icon.ico;." --hidden-import pystray._win32 --paths windows --paths . --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 windows/main.py
```

Run from source with `py windows\main.py`; add `--browser` to use your web browser. `--selftest` checks that everything starts and then exits. It also runs on Linux for development (`python3 windows/main.py`).

## Releasing

Releases cover both apps: see "Releasing" in the main [README](../README.md).

## How it works

`main.py` starts a small web server on 127.0.0.1 (`sb_server.py`) and shows its page (`ui/index.html`) in a WebView2 window. Audio is mixed in `sb_audio.py` with PortAudio (WASAPI) and numpy; decoding is in `sb_library.py` (miniaudio, or ffmpeg when present). The online sources are shared with the Linux mixer (`../amx_sounds.py`).
