# Mint Dictate Local USB

Deze map is bedoeld om direct op een USB-stick te zetten. De app gebruikt alleen lokale `faster-whisper` modellen en toont geen OpenAI-backend.

## Wat je op de andere laptop doet

1. Open een terminal in de USB-map.
2. Installeer de Linux-pakketten:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-dev build-essential python3-gi gir1.2-ayatanaappindicator3-0.1 xdotool libnotify-bin xclip playerctl libevdev-dev
```

3. Start de installatie:

```bash
chmod +x install.sh
./install.sh
```

## Wat het script doet

- Installeert de app in `~/.local/share/mint-dictate-local`
- Schrijft config naar `~/.config/mint-dictate-local/config.json`
- Maakt een `systemd --user` service aan
- Start die service meteen
- Downloadt direct de lokale modellen `small`, `medium` en `large-v3-turbo`

## Handige commando's achteraf

Service-status:

```bash
systemctl --user status mint-dictate-local.service
```

Logs:

```bash
journalctl --user -u mint-dictate-local.service -n 50 --no-pager
```

Config openen:

```bash
xdg-open ~/.config/mint-dictate-local/config.json
```

Service herstarten:

```bash
systemctl --user restart mint-dictate-local.service
```

## Standaardinstellingen

- backend: `local`
- modellen in de app: `small`, `medium`, `large-v3-turbo`
- standaardmodel: `small`
- taal: `nl`
- device: `cpu`
- compute type: `int8`

## Opmerking

De eerste installatie heeft internet nodig voor `apt`, `pip` en het downloaden van de Whisper-modellen. Daarna draait transcriptie lokaal.

Omdat nu alle drie de modellen vooraf worden gedownload, duurt `./install.sh` merkbaar langer en gebruikt het meer schijfruimte dan eerst.

Als `pip` faalt op `evdev` met een `pyproject.toml`/`building wheel` fout, installeer dan alsnog:

```bash
sudo apt install -y python3-dev build-essential libevdev-dev
./install.sh
```
