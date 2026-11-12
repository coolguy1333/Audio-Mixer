# Audio Mixer

A Wave Link–style software audio mixer for Linux (PipeWire / PulseAudio), built with GTK4 and libadwaita.
No special hardware needed.

- **Channels** (rows): virtual channels that apps are routed into (Games, Voice, Music, …), plus hardware inputs such as microphones.
- **Mixes** (columns): independent buses with their own level for every channel. Each mix can be sent to any number of output devices, and it can also be exposed as a **virtual microphone**, so Discord or OBS can record it.
- **System channel**: catches every sound that isn't assigned to another channel.
- **Apps page**: send any playing app to a channel (remembered), and choose the input that each recording app uses.
- **Virtual Stream Deck**: pages of keys for mute, volume, routing, system stats (CPU/GPU/RAM/network/disk), media control, launching apps, websites and commands. Icons, images and colours are customizable. It can pop out into a resizable, lockable, always-on-top window.
- **System tray**: closing the window keeps the mixer running in the tray.
- **OpenDeck plugin** (`opendeck/`): control the mixer from a real Stream Deck through [OpenDeck](https://github.com/nekename/OpenDeck).

Audio passes through untouched. At 100% faders with no effects on, the signal is bit-identical; the graph stays at the server's sample rate in 32-bit float. The added delay is about 9 ms (5 ms loopback buffers). Change it under **Settings ▸ Latency** (3 ms buffers give about 4 ms but run PipeWire at a 1 ms period).

### Effects

Every channel has an effect chain (the **Effects** tab when you click a channel). Every mix has one too, for its device outputs:

| Effect | Channels | Mixes | |
|---|---|---|---|
| Noise suppression | ✓ | | RNNoise, at full strength (adds 10 ms while on) |
| Noise gate | ✓ | | threshold, reduction, attack, hold, release |
| Low cut | ✓ | | 24 dB/octave high-pass |
| Equalizer | ✓ | ✓ | **Simple** (bass / mid / treble) or **Advanced**: 10 parametric bands on a draggable response curve, with presets |
| De-esser | ✓ | | split-band, so only the sibilant band is turned down |
| Compressor | ✓ | ✓ | ratio, knee, attack/release, automatic makeup gain |
| Creative Rack | ✓ | | see below |
| Limiter | ✓ | ✓ | 5 ms look-ahead, adjustable ceiling |
| Ear protection | app channels | ✓ | BS.1770 (LUFS) loudness limiter: a sudden loud video can't blast your ears |

The **Creative** tab is a pedalboard of twenty-six effects: Creative EQ (an amp-style Gain / Bass / Middle / Treble / Presence / Master stack), Overdrive, Tape Saturator, Bitcrusher, Chorus, Flanger, Phaser, Tremolo, Ring Modulator, Envelope Filter (auto-wah), Pitch Shifter, Delay (damping, ping-pong), Reverse Delay and Reverb (size, damping, predelay), plus the EasyEffects line-up: Auto Gain, Expander, Multiband Compressor, Multiband Gate, Maximizer, Filter, Bass Enhancer, Bass Loudness, Exciter, Crystalizer, Stereo Tools and Crossfeed. They are all built into `fx/amx_fx.c`, so EasyEffects and its plugin packages are not needed. Every unit is optional: a channel carries only the units you add. Drag a unit by its grip to change the order the audio takes through them. Each unit has its own power switch and RESET. Save a whole rack as a preset and load it onto any other channel. Presets live in `~/.config/audio-mixer/rack-presets.json` and import/export as files. "Open the rack in its own window" gives you a window to keep next to whatever you play into.

How it works: the effects are a small C plugin (`fx/amx_fx.c`, built automatically into `fx/amx_fx.so` on first start; needs `gcc`). Each channel with effects runs it in its own PipeWire filter-chain, which publishes the processed audio as a source named `amx_fx_…`. Knob moves are applied live. Only adding, removing or reordering units (or switching a channel's first / last effect on or off) rebuilds that channel's chain.

### Ducking

In a channel's **Channel settings**, turn on Ducking and choose which channels trigger it (e.g. duck *Music* while *Mic* or *Chat* is active). You can set how far it turns down, the trigger level and the hold time.

### Soundboard

The **Soundboard** page holds pads with a name, a waveform, play/stop and a stable 4-digit id. Right-click a pad (or ⋯) to trim it on the waveform and set its own volume. Pressing a pad again layers another copy instead of cutting the first one off. The whole board plays into one channel you choose (or a new "Soundboard" channel), so the mixer decides who hears it: your headphones, the stream, or the microphone mix your friends hear. Sounds are copied into `~/.config/audio-mixer/sounds/` (decoded copies are kept in `~/.cache/audio-mixer/pcm/` rather than in memory; safe to delete), and the whole board exports and imports as one `.zip` from **Settings ▸ Backups**. Needs `ffmpeg` for anything other than 16-bit WAV.

### Web Companion

**Settings ▸ Web Companion** serves the mixer to any phone or tablet on your network (port 8787 by default) as a mixing console:
- a channel strip per channel, with insert slots that light up for active effects (tap **FX** to switch any effect or rack unit), DUCK, a pan pot (drag sideways, double-tap to centre), an LED meter with peak hold, a long-throw fader with a dB scale and readout (double-tap for 0 dB), MUTE and a colour-coded name strip
- layer buttons along the top flip the faders between the channel masters and each mix's sends, the way an aux layer works on a desk; in a mix layer each strip has SEND ON/OFF
- a master section on the right with each mix's master fader, or, inside a mix layer, that mix's master plus a fader and mute for every output device
- soundboard pads, and scenes (profiles)

Changes appear both ways within a frame or two. The page is `web/companion.html`. It is off by default and can start with the mixer. **There is no password**: anyone who can reach the port can change your mixer, so leave it off on networks you don't control.

### Profiles

A profile is a whole mixer setup: channels, levels, mutes, routing and effects. Save, rename, switch, delete, import and export them in **Settings ▸ Profiles**. Switching keeps the edits you made to the profile you leave.

### More

- **Pan**: place any channel left or right (Channel settings ▸ Pan, double-click the value to centre). It applies in every mix. The near side stays at full level, so a centred channel is untouched.
- **Start at login** (Settings ▸ Startup): adds an autostart entry; by default it starts in the tray without opening the window. `audio-mixer --hidden` does the same by hand. If the mixer starts before PipeWire is ready, it waits for it instead of starting empty.
- **Muting a microphone** channel mutes the microphone itself, so apps that record it directly (Discord, OBS, a browser) go silent too, not only the mixes. Unmuting it from the desktop (e.g. the mic-mute key) unmutes the channel, and quitting the mixer gives the microphone back unmuted. The mute next to a mix's level only affects that mix.
- **Per-output levels**: each output device of a mix has its own volume and mute (headphones and speakers at once, at different levels).
- **MIDI instrument**: a virtual channel can host a FluidSynth SoundFont (`.sf2`) that every MIDI keyboard plays (Channel settings ▸ MIDI instrument). Needs `fluidsynth` and a SoundFont.
- **Deck keys** for all of this: play / stop a sound, switch an effect on or off, toggle ducking, switch profile.
- **Command line** for keybinds and scripts, talking to the running mixer (`audio-mixer ctl …` when installed from the AUR):

  ```sh
  python3 audio_mixer.py ctl play 4821          # soundboard (also: stop, toggle, stop-all)
  python3 audio_mixer.py ctl fx Mic denoise     # toggle (or: on / off); rack units by name: fx Mic reverb
  python3 audio_mixer.py ctl fx-set Mic gate Threshold -50
  python3 audio_mixer.py ctl profile Streaming
  python3 audio_mixer.py ctl pan Music L30      # also R30, C, or -100..100
  python3 audio_mixer.py ctl vol Music -5       # also: mute, mix-vol, mix-mute, duck, state
  ```

The feature set follows [Waveline](https://github.com/Nakildias/Waveline). This is an independent implementation; no Waveline code is used.

## Requirements

- PipeWire with `pipewire-pulse` (or PulseAudio), plus `pactl` and `parec`
- Python 3.10+, PyGObject, GTK 4, libadwaita 1.5+, pycairo
- For effects: `gcc` and `rnnoise` (without rnnoise everything except noise suppression works)
- Optional: `ffmpeg` (soundboard formats), `fluidsynth` (MIDI instruments), KDE Plasma (KWin) for the deck's always-on-top and hide-from-taskbar options, `nvidia-smi` for NVIDIA GPU stats

Arch / CachyOS: `sudo pacman -S python-gobject gtk4 libadwaita python-cairo libpulse gcc rnnoise ffmpeg fluidsynth`

## Install (Arch / CachyOS)

From the AUR (`audio-mixer-git`):

```sh
paru -S audio-mixer-git     # or: yay -S audio-mixer-git
```

Or build it from this repo: `cd packaging/aur && makepkg -si`. That installs the `audio-mixer` command, a menu entry and the icon, and builds the effects plugin at package time. The OpenDeck plugin is at `/usr/share/audio-mixer/opendeck/install.sh`.

## Run

```sh
python3 audio_mixer.py
```

Optional desktop launcher (replace the path):

```ini
# ~/.local/share/applications/io.github.lawsonf.AudioMixer.desktop
[Desktop Entry]
Type=Application
Name=Audio Mixer
Exec=python3 /path/to/audio_mixer.py
Icon=io.github.lawsonf.AudioMixer
Categories=AudioVideo;Audio;Mixer;
```

Then copy `assets/io.github.lawsonf.AudioMixer.svg` to `~/.local/share/icons/hicolor/scalable/apps/`.

Settings are saved in `~/.config/audio-mixer/config.json`. If that file is ever damaged, the mixer keeps it as `config.json.broken-<date>`, starts with a fresh setup and tells you. Quitting from the tray or sidebar removes all virtual devices and restores your default output and microphone.

Closing the window keeps the mixer running so your audio stays routed. Without a system tray, open it again from the app menu.

## OpenDeck plugin

```sh
sh opendeck/install.sh
```

Restart OpenDeck afterwards. The plugin talks to the mixer through a local socket at `$XDG_RUNTIME_DIR/audio-mixer.sock`, which takes JSON lines. The protocol is documented in `ControlServer` in `audio_mixer.py`.

## License

MIT, see [LICENSE](LICENSE).
