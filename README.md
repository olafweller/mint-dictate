# Mint Dictate

Mint Dictate is a Linux Mint X11 tray app that turns a configurable global hotkey into desktop dictation. It records your voice, transcribes it with OpenAI speech-to-text or a local Parakeet model, copies the transcript to the clipboard, and automatically pastes it into the active text field.

It is built for practical dictation on Cinnamon/X11. Browser address bars, VS Code, chat apps, and standard text editors are the main target.

Mint Dictate is free and open source. If it saves you time and makes Linux Mint more fun now that you can talk to your computer, consider supporting the project on Ko-fi:

https://ko-fi.com/mintdictate

## What's Included

- OpenAI speech-to-text with your own OpenAI API key
- Local speech-to-text with `nemo-parakeet-tdt-0.6b-v3`
- Configurable global hotkey to start and stop recording
- Linux Mint tray menu with status, settings, model selection, last transcript, word replacements, about, and donate
- GTK settings window for backend, OpenAI API key, model, language, hotkey, max recording time, and word replacements
- Clipboard-first workflow, then automatic `Ctrl+V` paste
- Optional media auto-pause during recording when `playerctl` is installed
- `systemd --user` service for auto-start on login

## Not Included

- Venice API
- Whisper local models such as `large-v3-turbo`, `medium`, or `small`
- Telemetry
- Paid support, roadmap promises, or a guaranteed release schedule

## Limitations

- X11 only. Wayland is not a supported target.
- Paste is simulated with `xdotool`, so the target text field must still have focus when transcription finishes.
- Media auto-pause only works for players that expose MPRIS controls.
- OpenAI mode uses your own OpenAI API key.
- Local mode downloads the Parakeet model on first use.

## Install

Download and install the latest `.deb`:

```bash
wget https://github.com/olafweller/mint-dictate/raw/main/mint-dictate.deb
sudo apt install ./mint-dictate.deb
mint-dictate-setup
```

The setup command creates your user config, enables the user service, and starts Mint Dictate.

## Configure OpenAI

Mint Dictate starts with local Parakeet by default. To use OpenAI:

1. Open the tray menu.
2. Choose `Settings`.
3. Select `OpenAI API`.
4. Paste your OpenAI API key into `API Key`.
5. Save.

Your config is stored in:

```text
~/.config/mint-dictate/config.json
```

## Requirements

The `.deb` package installs the system packages it needs through APT dependencies. For source installs, install:

```bash
sudo apt install python3 python3-venv python3-dev build-essential python3-gi gir1.2-ayatanaappindicator3-0.1 xdotool libnotify-bin xclip playerctl libevdev-dev
```

## Source Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/mint-dictate
cp config.example.json ~/.config/mint-dictate/config.json
python3 mint_dictate.py
```

The app uses system `python3` so GTK/AppIndicator is available, and loads the project `.venv` packages automatically.

## Useful Commands

Service status:

```bash
systemctl --user status mint-dictate.service
```

Logs:

```bash
journalctl --user -u mint-dictate.service -n 50 --no-pager
```

Config:

```bash
xdg-open ~/.config/mint-dictate/config.json
```

## License

Released under the MIT License.
