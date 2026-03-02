# Mint Dictate

Mint Dictate is a Linux Mint X11 tray app that turns a configurable global hotkey into desktop dictation. It records your voice, sends the audio to OpenAI speech-to-text, copies the transcript to the clipboard, and pastes it into the active text field with `Ctrl+V`.

It is built for practical dictation on Cinnamon/X11. Browser address bars, VS Code, chat apps, and standard text editors are the main target.

## Features

- Configurable global hotkey toggles start and stop
- Native Linux tray integration via GTK AppIndicator
- Tray menu with: start/stop, status, transcript preview, settings, about, restart
- GTK settings window for API key, model, language, hotkey, and max recording time
- Automatic language detection, specific language selection, or custom language code
- Clipboard-first workflow, then automatic `Ctrl+V` paste
- Optional media auto-pause during recording when `playerctl` is installed
- `systemd --user` service for auto-start on login

## Limitations

- X11 only. Wayland is not a supported target.
- Paste is simulated with `xdotool`, so the target text field must still have focus when transcription finishes.
- Media auto-pause only works for players that expose MPRIS controls.
- This app uses your own OpenAI API key.

## Requirements

- Linux Mint on X11
- `python3`, `python3-venv`
- `python3-gi`
- `gir1.2-ayatanaappindicator3-0.1`
- `xdotool`
- `libnotify-bin`
- `xclip`
- optional: `playerctl`

Install system packages:

```bash
sudo apt install python3 python3-venv python3-gi gir1.2-ayatanaappindicator3-0.1 xdotool libnotify-bin xclip playerctl
```

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/mint-dictate
cp config.example.json ~/.config/mint-dictate/config.json
```

Then add your OpenAI API key to `~/.config/mint-dictate/config.json`.

If that file does not exist, the app can still fall back to `config.json` in the project directory, but the user config path is the intended location.

Defaults:

- `transcription_model`: `gpt-4o-mini-transcribe`
- `language`: `null` for automatic language detection
- `hotkey`: configurable in Settings

Logs are written to `~/.cache/mint-dictate.log`.

## Running

Start manually:

```bash
python3 mint_dictate.py
```

The app uses system `python3` so GTK/AppIndicator is available, and loads the project `.venv` packages automatically.

Start as a user service:

```bash
mkdir -p ~/.config/systemd/user
cp mint-dictate.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mint-dictate.service
```

## How It Works

- Press your configured hotkey to start recording
- Press the same hotkey again to stop recording and start transcription
- The transcript is copied to your clipboard
- After a short delay, `Ctrl+V` is sent to the active window
- If `playerctl` is installed, currently playing media is paused during recording and resumed when recording stops

Tray icon colors:

- gray: idle
- red: recording
- blue: transcribing
- yellow: error

## Tray Menu

The tray menu is designed for normal users, not just debugging:

- Start or stop recording
- View current status
- View the last transcript preview
- Copy the last transcript again
- Open the settings window
- Open the about window
- Restart Mint Dictate

## Settings

The GTK settings window lets users change the core behavior without editing JSON:

- OpenAI API key
- Transcription model
- Language mode:
  - Auto Detect
  - Specific Language
  - Custom Code (advanced)
- Hotkey capture button
- Maximum recording duration

Advanced options such as `paste_delay_seconds`, `recording_path`, and `pause_media_during_recording` remain available in the config file.

## Troubleshooting

- No tray icon after login:
  Run `systemctl --user status mint-dictate.service`
- Transcription fails:
  Run `journalctl --user -u mint-dictate.service -n 50 --no-pager`
- Paste does not work:
  Make sure the target field still has focus and `xdotool` works in your X11 session
- Media does not pause:
  Check that `playerctl -l` shows your player and that the app exposes MPRIS controls
- Clipboard does not work:
  Make sure `xclip` is installed

## Publishing Notes

This repo already includes:

- `.gitignore`
- `LICENSE`
- `config.example.json`
- `mint-dictate.service`

Before publishing, the best remaining polish is still:

- add a screenshot of the tray menu and settings window
- optionally add a short GIF of the dictation flow
