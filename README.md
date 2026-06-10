# Mint Dictate

Mint Dictate lets you dictate text anywhere on Linux Mint. Press a hotkey, speak, and the transcript is pasted into the text field you were using.

It is made for Linux Mint Cinnamon on X11 and works well in browser text fields, chat apps, VS Code, notes, email, and other everyday writing places.

Mint Dictate is free and open source. If it saves you time and makes Linux Mint more fun now that you can talk to your computer, consider supporting the project on Ko-fi:

https://ko-fi.com/mintdictate

## Install

Download and install the latest `.deb`:

```bash
wget https://github.com/olafweller/mint-dictate/raw/main/mint-dictate.deb
sudo apt install ./mint-dictate.deb
mint-dictate-setup
```

After setup, Mint Dictate starts as a tray app. Use the tray menu to open `Settings`, switch between local and OpenAI transcription, change the hotkey, and add word replacements.

## How It Works

- Press the configured hotkey to start recording.
- Press it again to stop.
- Mint Dictate transcribes the recording.
- The transcript is copied to your clipboard.
- `Ctrl+V` is sent to paste the text into the active field.

By default, Mint Dictate uses local Parakeet speech-to-text. The local model downloads on first use.

## OpenAI Option

You can also use OpenAI speech-to-text with your own OpenAI API key:

1. Open the tray menu.
2. Choose `Settings`.
3. Select `OpenAI API`.
4. Paste your OpenAI API key into `API Key`.
5. Save.

Your settings are stored locally in:

```text
~/.config/mint-dictate/config.json
```

## Privacy

- Local mode transcribes on your computer after the Parakeet model has been downloaded.
- OpenAI mode sends the recorded audio to OpenAI for transcription using your own API key.
- Your API key is stored in your local config file.
- Mint Dictate does not include telemetry or usage tracking.

## Requirements

- Linux Mint Cinnamon on X11
- A working microphone
- Internet access for installation and first local model download
- For OpenAI mode: your own OpenAI API key

Wayland is not supported. Paste is simulated with `xdotool`, so the target text field must still have focus when transcription finishes.

## Troubleshooting

Service status:

```bash
systemctl --user status mint-dictate.service
```

Logs:

```bash
journalctl --user -u mint-dictate.service -n 50 --no-pager
```

App log:

```bash
tail -n 80 ~/.cache/mint-dictate.log
```

Test the local Parakeet model startup:

```bash
/opt/mint-dictate/.venv/bin/python /opt/mint-dictate/mint_dictate.py --local-transcribe-server nemo-parakeet-tdt-0.6b-v3 cpu int8
```

Config:

```bash
xdg-open ~/.config/mint-dictate/config.json
```

## Source Install

Most users should use the `.deb` above. For source installs:

```bash
sudo apt install python3 python3-venv python3-dev build-essential python3-gi gir1.2-ayatanaappindicator3-0.1 xdotool libnotify-bin xclip playerctl libevdev-dev
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/mint-dictate
cp config.example.json ~/.config/mint-dictate/config.json
python3 mint_dictate.py
```

## License

Released under the MIT License.
