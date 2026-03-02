#!/usr/bin/env python3

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

APP_NAME = "Mint Dictate"
APP_ID = "mint-dictate"
APP_AUTHOR = "Olaf Weller"
APP_WEBSITE = "https://x.com/WellerOlaf"
APP_DESCRIPTION = "Dictate text anywhere in Linux Mint using your OpenAI API key."
APP_DIR = Path(__file__).resolve().parent
for site_packages in sorted((APP_DIR / ".venv" / "lib").glob("python*/site-packages")):
    site_path = str(site_packages)
    if site_path not in sys.path:
        sys.path.insert(0, site_path)

import numpy as np
import pyperclip
import sounddevice as sd
import soundfile as sf
from openai import OpenAI
from PIL import Image, ImageDraw
from pynput import keyboard

try:
    import gi

    gi.require_version("AyatanaAppIndicator3", "0.1")
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import AyatanaAppIndicator3, Gdk, GLib, Gtk

    APPINDICATOR_AVAILABLE = True
except Exception:
    APPINDICATOR_AVAILABLE = False
    gi = None
    AyatanaAppIndicator3 = None
    Gdk = None
    GLib = None
    Gtk = None

try:
    import pystray
except Exception:
    pystray = None

PROJECT_CONFIG_PATH = Path.cwd() / "config.json"
USER_CONFIG_PATH = Path.home() / ".config" / "mint-dictate" / "config.json"
LEGACY_USER_CONFIG_PATH = Path.home() / ".config" / "linux-mint-speech-to-text" / "config.json"
LOG_PATH = Path.home() / ".cache" / "mint-dictate.log"
ICON_CACHE_DIR = Path.home() / ".cache" / "mint-dictate-icons"
DEFAULT_CONFIG = {
    "openai_api_key": "",
    "transcription_model": "gpt-4o-mini-transcribe",
    "language": None,
    "sample_rate": 16000,
    "channels": 1,
    "max_recording_seconds": 300,
    "hotkey": "<ctrl>+<alt>+m",
    "paste_delay_seconds": 0.15,
    "recording_path": str(Path(tempfile.gettempdir()) / "mint-dictate.wav"),
    "pause_media_during_recording": True,
}
TRANSCRIPTION_MODELS = [
    "gpt-4o-mini-transcribe",
    "gpt-4o-transcribe",
    "gpt-4o-transcribe-diarize",
    "whisper-1",
]
LANGUAGE_OPTIONS = [
    ("ar", "Arabic"),
    ("zh", "Chinese"),
    ("nl", "Dutch"),
    ("en", "English"),
    ("fi", "Finnish"),
    ("fr", "French"),
    ("de", "German"),
    ("hi", "Hindi"),
    ("it", "Italian"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("no", "Norwegian"),
    ("pl", "Polish"),
    ("pt", "Portuguese"),
    ("ru", "Russian"),
    ("es", "Spanish"),
    ("sv", "Swedish"),
    ("tr", "Turkish"),
    ("uk", "Ukrainian"),
]
STATE_COLORS = {
    "idle": "#7a7a7a",
    "recording": "#d73737",
    "transcribing": "#2d7dd2",
    "error": "#d9a404",
}

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["notify-send", "--app-name=Mint Dictate", title, message],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


def send_ctrl_v() -> None:
    subprocess.run(
        ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def copy_to_clipboard(text: str) -> None:
    try:
        pyperclip.copy(text)
        return
    except Exception:
        logging.exception("pyperclip copy failed, trying xclip fallback")

    last_error = None
    for command in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        try:
            subprocess.run(
                command,
                input=text,
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"Clipboard copy mislukt: {last_error}")


def get_playerctl_path() -> str | None:
    return shutil.which("playerctl")


def launch_path(path: Path) -> None:
    try:
        subprocess.Popen(
            ["xdg-open", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        notify(APP_NAME, f"Kon {path} niet openen: xdg-open ontbreekt.")


def make_icon_image(color: str, size: int = 64) -> Image.Image:
    scale = 4
    large_size = size * scale
    image = Image.new("RGBA", (large_size, large_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    inset = 8 * scale
    draw.ellipse((inset, inset, large_size - inset, large_size - inset), fill=color)
    image = image.resize((size, size), Image.LANCZOS)
    return image


def build_icon_paths() -> dict[str, str]:
    paths = {}
    for state, color in STATE_COLORS.items():
        path = ICON_CACHE_DIR / f"{state}.png"
        make_icon_image(color).save(path)
        paths[state] = str(path)
    return paths


def load_config() -> dict:
    config = DEFAULT_CONFIG.copy()
    for path in (USER_CONFIG_PATH, LEGACY_USER_CONFIG_PATH, PROJECT_CONFIG_PATH):
        if not path.exists():
            continue

        content = path.read_text(encoding="utf-8").strip()
        if not content:
            logging.info("Skipping empty config file: %s", path)
            continue

        loaded = json.loads(content)
        if "transcription_model" not in loaded and "whisper_model" in loaded:
            loaded["transcription_model"] = loaded["whisper_model"]
        config.update(loaded)
        logging.info("Loaded config from %s", path)
    return config


def save_user_config(config: dict) -> None:
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(config)
    payload.pop("whisper_model", None)
    USER_CONFIG_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logging.info("Saved config to %s", USER_CONFIG_PATH)


class SettingsWindow:
    MODIFIER_TOKENS = {
        Gdk.KEY_Control_L: "<ctrl>",
        Gdk.KEY_Control_R: "<ctrl>",
        Gdk.KEY_Alt_L: "<alt>",
        Gdk.KEY_Alt_R: "<alt>",
        Gdk.KEY_Shift_L: "<shift>",
        Gdk.KEY_Shift_R: "<shift>",
        Gdk.KEY_Super_L: "<super>",
        Gdk.KEY_Super_R: "<super>",
        Gdk.KEY_Meta_L: "<super>",
        Gdk.KEY_Meta_R: "<super>",
    } if Gdk else {}

    def __init__(self, app: "MintDictateApp") -> None:
        self.app = app
        self.captured_hotkey = app.config.get("hotkey", DEFAULT_CONFIG["hotkey"])
        self.capture_dialog = None
        self.capture_label = None

        self.window = Gtk.Window(title=f"{APP_NAME} Settings")
        self.window.set_default_size(560, 360)
        self.window.set_border_width(16)
        self.window.connect("delete-event", self._on_delete)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.window.add(outer)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        outer.pack_start(grid, True, True, 0)

        self.api_key_entry = Gtk.Entry()
        self.api_key_entry.set_visibility(False)
        self.api_key_entry.set_invisible_char("*")

        self.model_combo = Gtk.ComboBoxText()
        for model in TRANSCRIPTION_MODELS:
            self.model_combo.append_text(model)

        self.language_mode_combo = Gtk.ComboBoxText()
        self.language_mode_combo.append("auto", "Auto Detect (Recommended)")
        self.language_mode_combo.append("specific", "Specific Language")
        self.language_mode_combo.append("custom", "Custom Code (Advanced)")
        self.language_mode_combo.connect("changed", self._on_language_mode_changed)

        self.language_combo = Gtk.ComboBoxText()
        for code, label in LANGUAGE_OPTIONS:
            self.language_combo.append(code, label)

        self.custom_language_entry = Gtk.Entry()
        self.custom_language_entry.set_placeholder_text("Example: nl, en, de, fr")

        language_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        language_box.pack_start(self.language_mode_combo, False, False, 0)
        language_box.pack_start(self.language_combo, False, False, 0)
        language_box.pack_start(self.custom_language_entry, False, False, 0)
        self.language_box = language_box

        self.hotkey_value_label = Gtk.Label()
        self.hotkey_value_label.set_xalign(0)
        self.capture_button = Gtk.Button(label="Capture Hotkey...")
        self.capture_button.connect("clicked", self._on_capture_hotkey)
        hotkey_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hotkey_box.pack_start(self.hotkey_value_label, True, True, 0)
        hotkey_box.pack_end(self.capture_button, False, False, 0)
        self.hotkey_box = hotkey_box

        self.max_recording_entry = Gtk.SpinButton()
        self.max_recording_entry.set_range(1, 3600)
        self.max_recording_entry.set_increments(1, 10)

        rows = [
            ("OpenAI API Key", self.api_key_entry),
            ("Transcription Model", self.model_combo),
            ("Language", self.language_box),
            ("Hotkey", self.hotkey_box),
            ("Max Recording (sec)", self.max_recording_entry),
        ]
        for index, (label_text, widget) in enumerate(rows):
            label = Gtk.Label(label=label_text)
            label.set_xalign(0)
            grid.attach(label, 0, index, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, index, 1, 1)

        helper_label = Gtk.Label(label="Advanced options remain available in the config file.")
        helper_label.set_xalign(0)
        outer.pack_start(helper_label, False, False, 0)

        self.message_label = Gtk.Label()
        self.message_label.set_xalign(0)
        outer.pack_start(self.message_label, False, False, 0)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.pack_end(button_box, False, False, 0)

        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", self._on_cancel)
        button_box.pack_end(cancel_button, False, False, 0)

        save_button = Gtk.Button(label="Save")
        save_button.connect("clicked", self._on_save)
        button_box.pack_end(save_button, False, False, 0)

        self.load_from_app_config()

    def show(self) -> None:
        self.load_from_app_config()
        self.message_label.set_text("")
        self.window.show_all()
        self._update_language_visibility()
        self.window.present()

    def load_from_app_config(self) -> None:
        config = self.app.config
        self.api_key_entry.set_text(config.get("openai_api_key", ""))
        self._set_model(config.get("transcription_model", DEFAULT_CONFIG["transcription_model"]))
        self._set_language_state(config.get("language"))
        self.captured_hotkey = config.get("hotkey", DEFAULT_CONFIG["hotkey"])
        self.hotkey_value_label.set_text(self.captured_hotkey)
        self.max_recording_entry.set_value(float(config.get("max_recording_seconds", DEFAULT_CONFIG["max_recording_seconds"])))

    def _set_model(self, model_name: str) -> None:
        if model_name not in TRANSCRIPTION_MODELS:
            self.model_combo.append_text(model_name)
        items = list(TRANSCRIPTION_MODELS)
        if model_name not in items:
            items.append(model_name)
        self.model_combo.set_active(items.index(model_name))

    def _current_model(self) -> str:
        model = self.model_combo.get_active_text()
        return model or DEFAULT_CONFIG["transcription_model"]

    def _set_language_state(self, language_code) -> None:
        if not language_code:
            self.language_mode_combo.set_active_id("auto")
            self.language_combo.set_active_id("en")
            self.custom_language_entry.set_text("")
        else:
            known_codes = {code for code, _label in LANGUAGE_OPTIONS}
            if language_code in known_codes:
                self.language_mode_combo.set_active_id("specific")
                self.language_combo.set_active_id(language_code)
                self.custom_language_entry.set_text("")
            else:
                self.language_mode_combo.set_active_id("custom")
                self.custom_language_entry.set_text(language_code)
        self._update_language_visibility()

    def _on_language_mode_changed(self, _widget) -> None:
        self._update_language_visibility()

    def _update_language_visibility(self) -> None:
        mode = self.language_mode_combo.get_active_id() or "auto"
        if mode == "specific":
            self.language_combo.show()
            self.custom_language_entry.hide()
        elif mode == "custom":
            self.language_combo.hide()
            self.custom_language_entry.show()
        else:
            self.language_combo.hide()
            self.custom_language_entry.hide()

    def _set_error(self, message: str) -> None:
        self.message_label.set_markup(f'<span foreground="#b00020">{GLib.markup_escape_text(message)}</span>')

    def _set_success(self, message: str) -> None:
        self.message_label.set_markup(f'<span foreground="#0b6e4f">{GLib.markup_escape_text(message)}</span>')

    def _on_capture_hotkey(self, _button) -> None:
        if self.capture_dialog is not None:
            self.capture_dialog.present()
            return

        dialog = Gtk.Window(title="Capture Hotkey")
        dialog.set_modal(True)
        dialog.set_transient_for(self.window)
        dialog.set_border_width(16)
        dialog.set_default_size(360, 100)
        dialog.connect("delete-event", self._close_capture_dialog)
        dialog.connect("key-press-event", self._on_capture_key_press)

        label = Gtk.Label(label="Press your new shortcut. Esc cancels.")
        label.set_xalign(0)
        dialog.add(label)
        dialog.show_all()
        dialog.present()
        dialog.grab_focus()

        self.capture_dialog = dialog
        self.capture_label = label

    def _close_capture_dialog(self, *_args):
        if self.capture_dialog is not None:
            self.capture_dialog.destroy()
        self.capture_dialog = None
        self.capture_label = None
        return True

    def _on_capture_key_press(self, _widget, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self._close_capture_dialog()
            return True

        hotkey = self._event_to_hotkey(event)
        if not hotkey:
            if self.capture_label is not None:
                self.capture_label.set_text("Use at least one modifier plus one non-modifier key.")
            return True

        self.captured_hotkey = hotkey
        self.hotkey_value_label.set_text(hotkey)
        self._close_capture_dialog()
        self._set_success("Hotkey captured.")
        return True

    def _event_to_hotkey(self, event) -> str | None:
        state = event.state
        modifiers = []
        if state & Gdk.ModifierType.CONTROL_MASK:
            modifiers.append("<ctrl>")
        if state & Gdk.ModifierType.MOD1_MASK:
            modifiers.append("<alt>")
        if state & Gdk.ModifierType.SHIFT_MASK:
            modifiers.append("<shift>")
        if state & Gdk.ModifierType.SUPER_MASK:
            modifiers.append("<super>")

        if not modifiers:
            return None

        if event.keyval in self.MODIFIER_TOKENS:
            return None

        key_name = Gdk.keyval_name(event.keyval)
        key_token = self._normalize_key_name(key_name)
        if not key_token:
            return None
        return "+".join(modifiers + [key_token])

    def _normalize_key_name(self, key_name: str | None) -> str | None:
        if not key_name:
            return None
        name = key_name.lower()
        if name.startswith("kp_"):
            name = name[3:]

        aliases = {
            "return": "enter",
            "escape": "esc",
            "prior": "page_up",
            "next": "page_down",
        }
        name = aliases.get(name, name)

        if len(name) == 1 and re.match(r"[a-z0-9]", name):
            return name
        if re.fullmatch(r"f\d{1,2}", name):
            return f"<{name}>"
        if name in {"space", "tab", "enter", "esc", "backspace", "delete", "home", "end", "page_up", "page_down", "left", "right", "up", "down", "insert"}:
            return f"<{name}>"
        return None

    def _collect_language(self):
        mode = self.language_mode_combo.get_active_id() or "auto"
        if mode == "auto":
            return None
        if mode == "specific":
            language = self.language_combo.get_active_id()
            if not language:
                raise ValueError("Choose a language or switch to Auto Detect.")
            return language

        custom_code = self.custom_language_entry.get_text().strip().lower()
        if not custom_code:
            raise ValueError("Custom language code is required.")
        if not re.fullmatch(r"[a-z]{2,3}(-[a-z]{2})?", custom_code):
            raise ValueError("Custom language code should look like en, nl, de or pt-br.")
        return custom_code

    def _collect_config(self) -> dict:
        api_key = self.api_key_entry.get_text().strip()
        if not api_key:
            raise ValueError("OpenAI API key is required.")

        hotkey = (self.captured_hotkey or "").strip()
        if not hotkey:
            raise ValueError("Capture a hotkey before saving.")

        try:
            max_recording_seconds = int(self.max_recording_entry.get_value())
        except Exception as exc:
            raise ValueError("Max recording must be a whole number.") from exc
        if max_recording_seconds < 1:
            raise ValueError("Max recording must be at least 1 second.")

        merged = dict(self.app.config)
        merged.update(
            {
                "openai_api_key": api_key,
                "transcription_model": self._current_model(),
                "language": self._collect_language(),
                "max_recording_seconds": max_recording_seconds,
                "hotkey": hotkey,
            }
        )
        return merged

    def _on_save(self, _button) -> None:
        try:
            new_config = self._collect_config()
            save_user_config(new_config)
            self.app.apply_config(new_config)
            self._set_success("Settings saved.")
            notify(APP_NAME, "Settings saved.")
        except Exception as exc:
            logging.exception("Failed to save settings")
            self._set_error(str(exc))

    def _on_cancel(self, _button) -> None:
        self.window.hide()

    def _on_delete(self, *_args):
        self.window.hide()
        return True


class AppIndicatorUI:
    def __init__(self, app: "MintDictateApp") -> None:
        self.app = app
        self.icon_paths = build_icon_paths()
        self.indicator = AyatanaAppIndicator3.Indicator.new(
            APP_ID,
            self.icon_paths["idle"],
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title(APP_NAME)
        self.settings_window = SettingsWindow(app)
        self.about_window = self._build_about_window()

        self.menu = Gtk.Menu()
        self.toggle_item = Gtk.MenuItem(label="Start Recording")
        self.toggle_item.connect("activate", self._activate, self.app.toggle_recording)
        self.menu.append(self.toggle_item)

        self.status_item = Gtk.MenuItem(label="Status: Idle")
        self.status_item.set_sensitive(False)
        self.menu.append(self.status_item)

        self.transcript_item = Gtk.MenuItem(label="Last Transcript: None")
        self.transcript_item.set_sensitive(False)
        self.menu.append(self.transcript_item)

        self.copy_item = Gtk.MenuItem(label="Copy Last Transcript Again")
        self.copy_item.connect("activate", self._activate, self.app.copy_last_transcript)
        self.menu.append(self.copy_item)

        self.settings_item = Gtk.MenuItem(label="Settings")
        self.settings_item.connect("activate", self._activate, self.show_settings)
        self.menu.append(self.settings_item)

        self.about_item = Gtk.MenuItem(label="About")
        self.about_item.connect("activate", self._activate, self.show_about)
        self.menu.append(self.about_item)

        self.restart_item = Gtk.MenuItem(label="Restart Mint Dictate")
        self.restart_item.connect("activate", self._activate, self.app.restart_service)
        self.menu.append(self.restart_item)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        self.refresh()

    def _activate(self, _widget, callback) -> None:
        callback()

    def run(self) -> None:
        notify(APP_NAME, "Tray-app gestart.")
        Gtk.main()

    def stop(self) -> None:
        GLib.idle_add(Gtk.main_quit)

    def refresh(self) -> None:
        GLib.idle_add(self._refresh_on_main)

    def show_settings(self) -> None:
        GLib.idle_add(self._show_settings_on_main)

    def _show_settings_on_main(self) -> bool:
        self.settings_window.show()
        return False

    def show_about(self) -> None:
        GLib.idle_add(self._show_about_on_main)

    def _build_about_window(self) -> Gtk.Window:
        window = Gtk.Window(title=f"About {APP_NAME}")
        window.set_default_size(480, 260)
        window.set_border_width(16)
        window.connect("delete-event", self._hide_about_window)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        window.add(outer)

        title_label = Gtk.Label()
        title_label.set_markup(f'<span size="x-large" weight="bold">{APP_NAME}</span>')
        title_label.set_xalign(0)
        outer.pack_start(title_label, False, False, 0)

        version_label = Gtk.Label(label="Version 0.1.0")
        version_label.set_xalign(0)
        outer.pack_start(version_label, False, False, 0)

        description_label = Gtk.Label(
            label=(
                "Mint Dictate lets you dictate into text fields on Linux Mint and transcribes "
                "your speech through OpenAI speech-to-text using your own API key."
            )
        )
        description_label.set_xalign(0)
        description_label.set_line_wrap(True)
        outer.pack_start(description_label, False, False, 0)

        author_label = Gtk.Label(label=f"Made by {APP_AUTHOR}")
        author_label.set_xalign(0)
        outer.pack_start(author_label, False, False, 0)

        link_button = Gtk.LinkButton.new_with_label(APP_WEBSITE, "Creator on X")
        link_button.set_halign(Gtk.Align.START)
        outer.pack_start(link_button, False, False, 0)

        license_label = Gtk.Label(label="Released under the MIT License.")
        license_label.set_xalign(0)
        outer.pack_start(license_label, False, False, 0)

        close_button = Gtk.Button(label="Close")
        close_button.set_halign(Gtk.Align.END)
        close_button.connect("clicked", lambda _button: window.hide())
        outer.pack_end(close_button, False, False, 0)
        return window

    def _hide_about_window(self, *_args):
        self.about_window.hide()
        return True

    def _show_about_on_main(self) -> bool:
        self.about_window.show_all()
        self.about_window.present()
        return False

    def _refresh_on_main(self) -> bool:
        self.indicator.set_icon_full(self.icon_paths[self.app.state], self.app.status_label())
        self.indicator.set_title(f"{APP_NAME} ({self.app.status_label()})")
        self.toggle_item.set_label("Stop Recording" if self.app.is_recording else "Start Recording")
        self.status_item.set_label(f"Status: {self.app.status_label()}")
        transcript = self.app.last_transcript.strip()
        if transcript:
            preview = transcript[:40] + ("..." if len(transcript) > 40 else "")
            self.transcript_item.set_label(f"Last Transcript: {preview}")
            self.copy_item.set_sensitive(True)
        else:
            self.transcript_item.set_label("Last Transcript: None")
            self.copy_item.set_sensitive(False)
        return False


class PystrayUI:
    def __init__(self, app: "MintDictateApp") -> None:
        if pystray is None:
            raise RuntimeError("Geen bruikbare tray-backend beschikbaar.")
        self.app = app
        self.icon = pystray.Icon(APP_ID, make_icon_image(STATE_COLORS["idle"]), APP_NAME)
        self.backend_module = type(self.icon).__module__
        self.menu_supported = "_xorg" not in self.backend_module
        self.refresh()

    def run(self) -> None:
        if self.menu_supported:
            notify(APP_NAME, "Tray-app gestart.")
        else:
            notify(APP_NAME, "Tray-app gestart. Klikmenu niet beschikbaar op deze X11 backend. Gebruik de hotkey.")
        self.icon.run()

    def stop(self) -> None:
        self.icon.stop()

    def refresh(self) -> None:
        self.icon.icon = make_icon_image(STATE_COLORS[self.app.state])
        self.icon.title = self.app.tooltip_text()
        self.icon.menu = self._build_menu()
        if self.menu_supported:
            self.icon.update_menu()

    def show_settings(self) -> None:
        self.app.open_config()

    def _build_menu(self):
        if not self.menu_supported:
            return None
        return pystray.Menu(
            pystray.MenuItem(
                lambda item: "Stop Recording" if self.app.is_recording else "Start Recording",
                lambda icon, item: self.app.toggle_recording(),
            ),
            pystray.MenuItem(
                lambda item: f"Status: {self.app.status_label()}",
                lambda icon, item: None,
                enabled=False,
            ),
            pystray.MenuItem(
                lambda item: (
                    f"Last Transcript: {self.app.last_transcript[:40]}..."
                    if self.app.last_transcript and len(self.app.last_transcript) > 40
                    else f"Last Transcript: {self.app.last_transcript or 'None'}"
                ),
                lambda icon, item: None,
                enabled=False,
            ),
            pystray.MenuItem(
                "Copy Last Transcript Again",
                lambda icon, item: self.app.copy_last_transcript(),
                enabled=lambda item: bool(self.app.last_transcript),
            ),
            pystray.MenuItem("Settings", lambda icon, item: self.show_settings()),
            pystray.MenuItem("About", lambda icon, item: self.app.show_about()),
            pystray.MenuItem("Restart Mint Dictate", lambda icon, item: self.app.restart_service()),
        )


class MintDictateApp:
    def __init__(self) -> None:
        self.config = load_config()
        api_key = self.config["openai_api_key"] or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OpenAI API key ontbreekt in config.json of OPENAI_API_KEY.")

        self.client = OpenAI(api_key=api_key)
        self.state_lock = threading.Lock()
        self.audio_lock = threading.Lock()
        self.busy = False
        self.is_recording = False
        self.audio_chunks = []
        self.recording_started_at = 0.0
        self.recording_timer = None
        self.stream = None
        self.hotkey_listener = None
        self.state = "idle"
        self.last_error = ""
        self.last_transcript = ""
        self.paused_players = []
        self.ui = self._create_ui()

    def _create_ui(self):
        if APPINDICATOR_AVAILABLE:
            logging.info("Using AppIndicator GTK backend")
            return AppIndicatorUI(self)
        logging.info("Falling back to pystray backend")
        return PystrayUI(self)

    def run(self) -> None:
        logging.info("%s starting", APP_NAME)
        self._start_hotkey_listener()
        self.ui.run()

    def stop(self) -> None:
        if self.hotkey_listener:
            self.hotkey_listener.stop()
        self._cancel_timer()
        if self.is_recording:
            self._stop_recording_internal()
        self.ui.stop()

    def restart_service(self) -> None:
        logging.info("Restart requested from tray menu")
        subprocess.Popen(
            ["systemctl", "--user", "restart", "mint-dictate.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _start_hotkey_listener(self) -> None:
        combo = self.config["hotkey"]
        self.hotkey_listener = keyboard.GlobalHotKeys({combo: self.toggle_recording})
        self.hotkey_listener.start()

    def _restart_hotkey_listener(self) -> None:
        if self.hotkey_listener:
            self.hotkey_listener.stop()
        self._start_hotkey_listener()

    def apply_config(self, new_config: dict) -> None:
        self.config = DEFAULT_CONFIG.copy() | self.config | new_config
        self.client = OpenAI(api_key=self.config["openai_api_key"])
        self._restart_hotkey_listener()
        self.ui.refresh()
        logging.info("Applied updated settings")

    def open_config(self) -> None:
        config_path = USER_CONFIG_PATH if USER_CONFIG_PATH.exists() else PROJECT_CONFIG_PATH
        launch_path(config_path)

    def open_log(self) -> None:
        launch_path(LOG_PATH)

    def show_settings(self) -> None:
        self.ui.show_settings()

    def copy_last_transcript(self) -> None:
        if not self.last_transcript:
            return
        copy_to_clipboard(self.last_transcript)
        notify(APP_NAME, "Laatste transcript opnieuw naar clipboard gekopieerd.")

    def show_about(self) -> None:
        language_value = self.config.get("language") or "auto"
        message = (
            f"{APP_DESCRIPTION}\n"
            f"Made by {APP_AUTHOR}.\n"
            f"Hotkey: {self.config['hotkey']}\n"
            f"Model: {self.config['transcription_model']}\n"
            f"Language: {language_value}"
        )
        notify(APP_NAME, message)

    def _detect_playing_players(self) -> list[str]:
        if not self.config.get("pause_media_during_recording", True):
            return []
        playerctl_path = get_playerctl_path()
        if not playerctl_path:
            return []


        try:
            players_result = subprocess.run(
                [playerctl_path, "-l"],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:
            logging.exception("Failed to list media players")
            return []

        players = []
        for player in players_result.stdout.splitlines():
            player = player.strip()
            if not player:
                continue
            try:
                status_result = subprocess.run(
                    [playerctl_path, "-p", player, "status"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            except Exception:
                continue
            if status_result.returncode == 0 and status_result.stdout.strip() == "Playing":
                players.append(player)
        return players

    def _pause_media_for_recording(self) -> None:
        self.paused_players = self._detect_playing_players()
        playerctl_path = get_playerctl_path()
        if not playerctl_path:
            self.paused_players = []
            return

        for player in self.paused_players:
            subprocess.run(
                [playerctl_path, "-p", player, "pause"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if self.paused_players:
            logging.info("Paused media players: %s", ", ".join(self.paused_players))

    def _resume_paused_media(self) -> None:
        playerctl_path = get_playerctl_path()
        if not playerctl_path or not self.paused_players:
            self.paused_players = []
            return
        players_to_resume = self.paused_players
        self.paused_players = []
        for player in players_to_resume:
            subprocess.run(
                [playerctl_path, "-p", player, "play"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        logging.info("Resumed media players: %s", ", ".join(players_to_resume))

    def toggle_recording(self) -> None:
        with self.state_lock:
            if self.busy and not self.is_recording:
                notify(APP_NAME, "Nog bezig met transcriberen, wacht even.")
                return

            if self.is_recording:
                self._stop_recording_internal()
                return

            self._start_recording_internal()

    def _start_recording_internal(self) -> None:
        with self.audio_lock:
            self.audio_chunks = []

        try:
            self.stream = sd.InputStream(
                samplerate=self.config["sample_rate"],
                channels=self.config["channels"],
                dtype="float32",
                callback=self._audio_callback,
            )
            self.stream.start()
        except Exception as exc:
            logging.exception("Failed to start recording")
            self.busy = False
            self.is_recording = False
            self._set_state("error")
            self.last_error = str(exc)
            notify(APP_NAME, f"Opname starten mislukt: {exc}")
            return

        self.is_recording = True
        self.busy = True
        self.recording_started_at = time.time()
        self.last_error = ""
        self._set_state("recording")
        self._pause_media_for_recording()
        self._schedule_auto_stop()
        notify(APP_NAME, "Recording started.")

    def _stop_recording_internal(self) -> None:
        self._cancel_timer()
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

        self.is_recording = False
        self._resume_paused_media()
        self._set_state("transcribing")
        notify(APP_NAME, "Transcribing...")

        worker = threading.Thread(target=self._transcribe_and_paste, daemon=True)
        worker.start()

    def _schedule_auto_stop(self) -> None:
        duration = int(self.config["max_recording_seconds"])
        self.recording_timer = threading.Timer(duration, self._auto_stop_from_timer)
        self.recording_timer.daemon = True
        self.recording_timer.start()

    def _cancel_timer(self) -> None:
        if self.recording_timer:
            self.recording_timer.cancel()
            self.recording_timer = None

    def _auto_stop_from_timer(self) -> None:
        with self.state_lock:
            if self.is_recording:
                self._stop_recording_internal()
                notify(APP_NAME, "Opname automatisch gestopt na 5 minuten.")

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if status:
            notify(APP_NAME, f"Audio waarschuwing: {status}")
        with self.audio_lock:
            self.audio_chunks.append(indata.copy())

    def _transcribe_and_paste(self) -> None:
        try:
            audio_path = self._write_recording()
            text = self._transcribe(audio_path).strip()
            if not text:
                self._set_state("idle")
                self.busy = False
                notify(APP_NAME, "Geen tekst herkend.")
                return

            logging.info("Transcription succeeded with %s characters", len(text))
            self.last_transcript = text
            copy_to_clipboard(text)
            time.sleep(float(self.config["paste_delay_seconds"]))
            send_ctrl_v()

            self._set_state("idle")
            self.busy = False
            preview = text[:80] + ("..." if len(text) > 80 else "")
            notify(APP_NAME, f"Transcript geplakt: {preview}")
        except Exception as exc:
            logging.error("Transcription flow failed: %s", exc)
            logging.error(traceback.format_exc())
            self._set_state("error")
            self.busy = False
            self.last_error = str(exc)
            notify(APP_NAME, f"Transcriptie mislukt: {exc}")

    def _write_recording(self) -> str:
        with self.audio_lock:
            if not self.audio_chunks:
                raise RuntimeError("Er is geen audio opgenomen.")
            audio = np.concatenate(self.audio_chunks, axis=0)

        path = Path(self.config["recording_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(
            str(path),
            audio,
            self.config["sample_rate"],
        )
        return str(path)

    def _transcribe(self, audio_path: str) -> str:
        request = {
            "model": self.config["transcription_model"],
            "language": self.config.get("language") or None,
        }
        with open(audio_path, "rb") as fh:
            transcript = self.client.audio.transcriptions.create(
                file=fh,
                **{key: value for key, value in request.items() if value is not None},
            )
        return transcript.text

    def _set_state(self, state: str) -> None:
        self.state = state
        self.ui.refresh()

    def status_label(self) -> str:
        labels = {
            "idle": "Idle",
            "recording": "Recording",
            "transcribing": "Transcribing",
            "error": "Error",
        }
        return labels.get(self.state, self.state.title())

    def tooltip_text(self) -> str:
        lines = [APP_NAME, f"Status: {self.status_label()}", f"Hotkey: {self.config['hotkey']}"]
        if self.last_error:
            lines.append(f"Last error: {self.last_error[:60]}")
        elif self.last_transcript:
            lines.append(f"Last transcript: {self.last_transcript[:60]}")
        return "\n".join(lines)


def main() -> None:
    app = MintDictateApp()

    def handle_signal(signum, frame) -> None:
        app.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    app.run()


if __name__ == "__main__":
    main()
