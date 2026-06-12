#!/usr/bin/env python3

from __future__ import annotations

import gc
import json
import logging
import os
import re
import shutil
import signal
import socket
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
APP_WEBSITE = "https://github.com/olafweller/mint-dictate"
APP_DONATION_URL = "https://ko-fi.com/mintdictate"
APP_DESCRIPTION = "Dictate text anywhere in Linux Mint with OpenAI or local Parakeet speech-to-text."
APP_SUPPORT_TEXT = (
    "Mint Dictate is free and open source. If it saves you time and makes Linux Mint more fun "
    "now that you can talk to your computer, consider supporting the project on Ko-fi."
)
APP_DIR = Path(__file__).resolve().parent
for site_packages in sorted((APP_DIR / ".venv" / "lib").glob("python*/site-packages")):
    site_path = str(site_packages)
    if site_path not in sys.path:
        sys.path.insert(0, site_path)

import numpy as np
import pyperclip
import sounddevice as sd
import soundfile as sf
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
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

try:
    import onnx_asr
except Exception:
    onnx_asr = None

PROJECT_CONFIG_PATH = Path.cwd() / "config.json"
USER_CONFIG_PATH = Path.home() / ".config" / "mint-dictate" / "config.json"
LEGACY_USER_CONFIG_PATH = Path.home() / ".config" / "linux-mint-speech-to-text" / "config.json"
LOG_PATH = Path.home() / ".cache" / "mint-dictate.log"
ICON_CACHE_DIR = Path.home() / ".cache" / "mint-dictate-icons"


def env_with_local_cuda_libraries() -> dict[str, str]:
    env = os.environ.copy()
    cuda_library_dirs: list[str] = []
    for site_packages in sorted((APP_DIR / ".venv" / "lib").glob("python*/site-packages")):
        nvidia_dir = site_packages / "nvidia"
        for package_dir in ("cublas", "cudnn", "cuda_runtime"):
            library_dir = nvidia_dir / package_dir / "lib"
            if library_dir.is_dir():
                cuda_library_dirs.append(str(library_dir))

    if cuda_library_dirs:
        current_library_path = env.get("LD_LIBRARY_PATH", "")
        existing_dirs = [path for path in current_library_path.split(":") if path]
        env["LD_LIBRARY_PATH"] = ":".join([*cuda_library_dirs, *existing_dirs])

    return env


DEFAULT_CONFIG = {
    "transcription_backend": "local",
    "openai_api_key": "",
    "transcription_model": "nemo-parakeet-tdt-0.6b-v3",
    "language": None,
    "sample_rate": 16000,
    "channels": 1,
    "max_recording_seconds": 300,
    "hotkey": "<ctrl>+<alt>+m",
    "paste_delay_seconds": 0.15,
    "recording_path": str(Path(tempfile.gettempdir()) / "mint-dictate.wav"),
    "pause_media_during_recording": True,
    "stop_media_players": ["de.haeckerfelix.Shortwave"],
    "local_model_device": "cpu",
    "local_model_compute_type": "int8",
    "openai_request_timeout_seconds": 20,
    "openai_retry_window_seconds": 600,
    "openai_retry_poll_seconds": 3,
    "local_model_idle_timeout_seconds": 120,
    "local_transcription_stats": {},
    "replacement_rules": "",
}
OPENAI_CONNECTIVITY_HOST = "api.openai.com"
OPENAI_CONNECTIVITY_PORT = 443
OPENAI_TRANSCRIPTION_MODELS = [
    "gpt-4o-mini-transcribe",
    "gpt-4o-transcribe",
    "gpt-4o-transcribe-diarize",
    "whisper-1",
]
LOCAL_TRANSCRIPTION_MODELS = [
    "nemo-parakeet-tdt-0.6b-v3",
]
MODEL_LABELS = {
    "gpt-4o-mini-transcribe": "GPT-4o mini",
    "gpt-4o-transcribe": "GPT-4o Transcribe",
    "gpt-4o-transcribe-diarize": "GPT-4o Transcribe diarize",
    "whisper-1": "Whisper",
    "nemo-parakeet-tdt-0.6b-v3": "Parakeet v3 (CPU)",
}
TRANSCRIPTION_BACKENDS = [
    ("openai", "OpenAI API"),
    ("local", "Local model"),
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
    "waiting_network": "#f08c00",
    "error": "#d9a404",
}


class ApiNetworkUnavailableError(RuntimeError):
    pass

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


def launch_url(url: str) -> None:
    try:
        subprocess.Popen(
            ["xdg-open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        notify(APP_NAME, f"Kon {url} niet openen: xdg-open ontbreekt.")


def run_local_transcription_worker_server(
    model_name: str,
    device: str,
    compute_type: str,
) -> int:
    dependency_error = local_model_dependency_error(model_name)
    if dependency_error:
        print(json.dumps({"error": dependency_error}), flush=True)
        return 1

    try:
        model = onnx_asr.load_model(model_name, providers=["CPUExecutionProvider"])
        print(json.dumps({"ready": True}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            audio_path = str(request["audio_path"])
            text = str(model.recognize(audio_path)).strip()
            print(json.dumps({"text": text}), flush=True)
        return 0
    except Exception as exc:
        logging.exception("Local transcription worker failed")
        print(json.dumps({"error": friendly_local_worker_error(str(exc))}), flush=True)
        return 1


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


def models_for_backend(backend: str) -> list[str]:
    if backend == "local":
        return list(LOCAL_TRANSCRIPTION_MODELS)
    return list(OPENAI_TRANSCRIPTION_MODELS)


def backend_label(backend: str) -> str:
    for value, label in TRANSCRIPTION_BACKENDS:
        if value == backend:
            return label
    return backend


def model_label(model_name: str) -> str:
    return MODEL_LABELS.get(model_name, model_name)


def is_local_parakeet_model(model_name: str) -> bool:
    return model_name == "nemo-parakeet-tdt-0.6b-v3"


def local_model_dependency_error(model_name: str) -> str | None:
    if is_local_parakeet_model(model_name):
        if onnx_asr is None:
            return "Local Parakeet transcription requires the onnx-asr package."
        return None
    return f"Unsupported local model: {model_name}"


def friendly_local_worker_error(message: str) -> str:
    if "External data path does not exist" in message and "encoder-model.onnx.data" in message:
        return (
            "De lokale Parakeet modeldownload lijkt incompleet. "
            "Herstel dit met: mint-dictate-repair-local-model"
        )
    return message


def language_label(language_code: str | None) -> str:
    if not language_code:
        return "Auto Detect"
    for code, label in LANGUAGE_OPTIONS:
        if code == language_code:
            return label
    return language_code


def apply_transcription_corrections(text: str) -> str:
    return text


def replacement_rules_text(config: dict) -> str:
    return str(config.get("replacement_rules", DEFAULT_CONFIG["replacement_rules"]) or "").strip()


def parse_replacement_rules(raw_rules: str) -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []
    for line in raw_rules.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if "=>" not in entry:
            raise ValueError("Elke vervangregel moet 'bron => doel' gebruiken.")
        source, target = entry.split("=>", 1)
        source = source.strip()
        target = target.strip()
        if not source or not target:
            raise ValueError("Elke vervangregel moet zowel bron als doel bevatten.")
        rules.append((source, target))
    return rules


def format_replacement_rules(rules: list[tuple[str, str]]) -> str:
    return "\n".join(f"{source} => {target}" for source, target in rules)


def apply_configured_replacements(text: str, config: dict) -> str:
    corrected = apply_transcription_corrections(text)
    for source, target in parse_replacement_rules(replacement_rules_text(config)):
        corrected = re.sub(rf"\b{re.escape(source)}\b", target, corrected, flags=re.IGNORECASE)
    return corrected


def configured_stop_media_players(config: dict) -> set[str]:
    players = config.get("stop_media_players", DEFAULT_CONFIG["stop_media_players"])
    if isinstance(players, str):
        players = re.split(r"[\n,]", players)
    if not isinstance(players, list):
        return set()
    return {str(player).strip() for player in players if str(player).strip()}


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
        if loaded.get("transcription_model") in LOCAL_TRANSCRIPTION_MODELS and "transcription_backend" not in loaded:
            loaded["transcription_backend"] = "local"
        loaded.pop("venice_api_key", None)
        loaded.pop("venice_base_url", None)
        config.update(loaded)
        logging.info("Loaded config from %s", path)
    valid_backends = {backend for backend, _label in TRANSCRIPTION_BACKENDS}
    if config.get("transcription_backend") not in valid_backends:
        config["transcription_backend"] = DEFAULT_CONFIG["transcription_backend"]
    valid_models = models_for_backend(config["transcription_backend"])
    if config.get("transcription_model") not in valid_models:
        config["transcription_model"] = valid_models[0]
    return config


def save_user_config(config: dict) -> None:
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(config)
    payload.pop("whisper_model", None)
    payload.pop("venice_api_key", None)
    payload.pop("venice_base_url", None)
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
        self.replacement_rule_rows = []

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

        self.backend_combo = Gtk.ComboBoxText()
        for backend, label in TRANSCRIPTION_BACKENDS:
            self.backend_combo.append(backend, label)
        self.backend_combo.connect("changed", self._on_backend_changed)

        self.model_combo = Gtk.ComboBoxText()

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

        self.replacement_rules_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        replacement_scroller = Gtk.ScrolledWindow()
        replacement_scroller.set_min_content_height(140)
        replacement_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        replacement_scroller.add(self.replacement_rules_list)

        self.add_replacement_button = Gtk.Button(label="+ Add Replacement")
        self.add_replacement_button.connect("clicked", self._on_add_replacement_rule)

        replacement_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        replacement_box.pack_start(replacement_scroller, True, True, 0)
        replacement_box.pack_start(self.add_replacement_button, False, False, 0)
        self.replacement_rules_box = replacement_box

        self.hotkey_value_label = Gtk.Label()
        self.hotkey_value_label.set_xalign(0)
        self.capture_button = Gtk.Button(label="Capture Hotkey...")
        self.capture_button.connect("clicked", self._on_capture_hotkey)
        hotkey_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hotkey_box.pack_start(self.hotkey_value_label, True, True, 0)
        hotkey_box.pack_end(self.capture_button, False, False, 0)
        self.hotkey_box = hotkey_box

        self.max_recording_minutes_entry, self.max_recording_seconds_entry, self.max_recording_box = (
            self._build_duration_input()
        )
        self.local_idle_minutes_entry, self.local_idle_seconds_entry, self.local_idle_timeout_box = (
            self._build_duration_input()
        )

        rows = [
            ("Transcription Backend", self.backend_combo),
            ("API Key", self.api_key_entry),
            ("Transcription Model", self.model_combo),
            ("Language", self.language_box),
            ("Word Replacements", self.replacement_rules_box),
            ("Hotkey", self.hotkey_box),
            ("Max Recording", self.max_recording_box),
            ("Local Model Idle", self.local_idle_timeout_box),
        ]
        for index, (label_text, widget) in enumerate(rows):
            label = Gtk.Label(label=label_text)
            label.set_xalign(0)
            grid.attach(label, 0, index, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, index, 1, 1)

        helper_label = Gtk.Label(label="Set local idle to 0 min 0 sec to keep the model loaded until app exit.")
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

    def _build_duration_input(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        minutes_entry = Gtk.SpinButton()
        minutes_entry.set_range(0, 1440)
        minutes_entry.set_increments(1, 5)

        seconds_entry = Gtk.SpinButton()
        seconds_entry.set_range(0, 59)
        seconds_entry.set_increments(1, 10)

        box.pack_start(minutes_entry, False, False, 0)
        box.pack_start(Gtk.Label(label="min"), False, False, 0)
        box.pack_start(seconds_entry, False, False, 0)
        box.pack_start(Gtk.Label(label="sec"), False, False, 0)

        return minutes_entry, seconds_entry, box

    def _set_duration_input(self, minutes_entry, seconds_entry, total_seconds: int | float) -> None:
        total_seconds = max(0, int(total_seconds or 0))
        minutes, seconds = divmod(total_seconds, 60)
        minutes_entry.set_value(float(minutes))
        seconds_entry.set_value(float(seconds))

    def _collect_duration_input(self, minutes_entry, seconds_entry, field_name: str, minimum_seconds: int) -> int:
        try:
            minutes = int(minutes_entry.get_value())
            seconds = int(seconds_entry.get_value())
        except Exception as exc:
            raise ValueError(f"{field_name} must use whole minutes and seconds.") from exc

        total_seconds = (minutes * 60) + seconds
        if total_seconds < minimum_seconds:
            raise ValueError(f"{field_name} must be at least {minimum_seconds} second.")
        return total_seconds

    def show(self) -> None:
        self.load_from_app_config()
        self.message_label.set_text("")
        self.window.show_all()
        self._update_backend_visibility()
        self._update_language_visibility()
        self.window.present()

    def load_from_app_config(self) -> None:
        config = self.app.config
        self.backend_combo.set_active_id(config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]))
        self.api_key_entry.set_text(self._api_key_for_backend(self._current_backend()))
        self._set_model(config.get("transcription_model", DEFAULT_CONFIG["transcription_model"]))
        self._set_language_state(config.get("language"))
        self._load_replacement_rules(replacement_rules_text(config))
        self.captured_hotkey = config.get("hotkey", DEFAULT_CONFIG["hotkey"])
        self.hotkey_value_label.set_text(self.captured_hotkey)
        self._set_duration_input(
            self.max_recording_minutes_entry,
            self.max_recording_seconds_entry,
            config.get("max_recording_seconds", DEFAULT_CONFIG["max_recording_seconds"]),
        )
        self._set_duration_input(
            self.local_idle_minutes_entry,
            self.local_idle_seconds_entry,
            config.get(
                "local_model_idle_timeout_seconds",
                DEFAULT_CONFIG["local_model_idle_timeout_seconds"],
            ),
        )
        self._update_backend_visibility()

    def _clear_replacement_rule_rows(self) -> None:
        for row in self.replacement_rule_rows:
            self.replacement_rules_list.remove(row["container"])
        self.replacement_rule_rows = []

    def _add_replacement_rule_row(self, source: str = "", target: str = "") -> None:
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        source_entry = Gtk.Entry()
        source_entry.set_placeholder_text("Replace this")
        source_entry.set_text(source)
        source_entry.set_hexpand(True)

        arrow_label = Gtk.Label(label="->")
        arrow_label.set_xalign(0.5)

        target_entry = Gtk.Entry()
        target_entry.set_placeholder_text("With this")
        target_entry.set_text(target)
        target_entry.set_hexpand(True)

        remove_button = Gtk.Button(label="Remove")

        row = {
            "container": row_box,
            "source": source_entry,
            "target": target_entry,
            "remove": remove_button,
        }
        remove_button.connect("clicked", self._on_remove_replacement_rule, row)

        row_box.pack_start(source_entry, True, True, 0)
        row_box.pack_start(arrow_label, False, False, 0)
        row_box.pack_start(target_entry, True, True, 0)
        row_box.pack_start(remove_button, False, False, 0)

        self.replacement_rule_rows.append(row)
        self.replacement_rules_list.pack_start(row_box, False, False, 0)
        row_box.show_all()

    def _load_replacement_rules(self, raw_rules: str) -> None:
        self._clear_replacement_rule_rows()
        parsed_rules = parse_replacement_rules(raw_rules) if raw_rules else []
        if not parsed_rules:
            self._add_replacement_rule_row()
            return
        for source, target in parsed_rules:
            self._add_replacement_rule_row(source, target)

    def _collect_replacement_rules(self) -> str:
        lines = []
        for row in self.replacement_rule_rows:
            source = row["source"].get_text().strip()
            target = row["target"].get_text().strip()
            if not source and not target:
                continue
            if not source or not target:
                raise ValueError("Elke woordvervanging moet zowel een bron als vervanging hebben.")
            lines.append(f"{source} => {target}")
        replacement_rules = "\n".join(lines)
        parse_replacement_rules(replacement_rules)
        return replacement_rules

    def _on_add_replacement_rule(self, _button) -> None:
        self._add_replacement_rule_row()
        self.replacement_rules_list.show_all()

    def _on_remove_replacement_rule(self, _button, row: dict) -> None:
        if row not in self.replacement_rule_rows:
            return
        self.replacement_rule_rows.remove(row)
        self.replacement_rules_list.remove(row["container"])
        if not self.replacement_rule_rows:
            self._add_replacement_rule_row()
        self.replacement_rules_list.show_all()

    def _set_model(self, model_name: str) -> None:
        items = self._model_choices()
        self.model_combo.remove_all()
        for model in items:
            self.model_combo.append_text(model)
        if model_name not in items:
            self.model_combo.append_text(model_name)
            items.append(model_name)
        self.model_combo.set_active(items.index(model_name))

    def _current_model(self) -> str:
        model = self.model_combo.get_active_text()
        return model or DEFAULT_CONFIG["transcription_model"]

    def _current_backend(self) -> str:
        return self.backend_combo.get_active_id() or DEFAULT_CONFIG["transcription_backend"]

    def _api_key_for_backend(self, backend: str) -> str:
        if backend == "openai":
            return self.app.config.get("openai_api_key", "")
        return ""

    def _model_choices(self) -> list[str]:
        return models_for_backend(self._current_backend())

    def _default_model_for_backend(self) -> str:
        return models_for_backend(self._current_backend())[0]

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

    def _on_backend_changed(self, _widget) -> None:
        model_name = self._current_model()
        if model_name not in self._model_choices():
            model_name = self._default_model_for_backend()
        self._set_model(model_name)
        self.api_key_entry.set_text(self._api_key_for_backend(self._current_backend()))
        self._update_backend_visibility()

    def _update_backend_visibility(self) -> None:
        using_api_backend = self._current_backend() == "openai"
        self.api_key_entry.set_sensitive(using_api_backend)
        if using_api_backend:
            self.api_key_entry.show()
        else:
            self.api_key_entry.hide()

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
        backend = self._current_backend()
        api_key = self.api_key_entry.get_text().strip()
        if backend == "openai" and not api_key:
            raise ValueError("OpenAI API key is required.")
        if backend == "local":
            dependency_error = local_model_dependency_error(self._current_model())
            if dependency_error:
                raise ValueError(dependency_error)

        hotkey = (self.captured_hotkey or "").strip()
        if not hotkey:
            raise ValueError("Capture a hotkey before saving.")

        max_recording_seconds = self._collect_duration_input(
            self.max_recording_minutes_entry,
            self.max_recording_seconds_entry,
            "Max recording",
            1,
        )
        local_idle_timeout_seconds = self._collect_duration_input(
            self.local_idle_minutes_entry,
            self.local_idle_seconds_entry,
            "Local model idle timeout",
            0,
        )

        replacement_rules = self._collect_replacement_rules()

        merged = dict(self.app.config)
        updates = {
            "transcription_backend": backend,
            "transcription_model": self._current_model(),
            "language": self._collect_language(),
            "replacement_rules": replacement_rules,
            "max_recording_seconds": max_recording_seconds,
            "local_model_idle_timeout_seconds": local_idle_timeout_seconds,
            "hotkey": hotkey,
        }
        if backend == "openai":
            updates["openai_api_key"] = api_key
        merged.update(updates)
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


class QuickAddReplacementWindow:
    def __init__(self, app: "MintDictateApp", parent: Gtk.Window | None = None) -> None:
        self.app = app
        self.parent = parent

        self.window = Gtk.Window(title="Add Word Replacement")
        self.window.set_default_size(420, 160)
        self.window.set_border_width(16)
        self.window.set_modal(True)
        if parent is not None:
            self.window.set_transient_for(parent)
        self.window.connect("delete-event", self._on_delete)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.window.add(outer)

        description = Gtk.Label(
            label="Add a word or phrase to replace in transcripts.",
        )
        description.set_xalign(0)
        outer.pack_start(description, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        outer.pack_start(grid, True, True, 0)

        source_label = Gtk.Label(label="Replace")
        source_label.set_xalign(0)
        grid.attach(source_label, 0, 0, 1, 1)

        self.source_entry = Gtk.Entry()
        self.source_entry.set_placeholder_text("Example: linux mint")
        self.source_entry.set_hexpand(True)
        self.source_entry.connect("activate", self._on_save)
        grid.attach(self.source_entry, 1, 0, 1, 1)

        target_label = Gtk.Label(label="With")
        target_label.set_xalign(0)
        grid.attach(target_label, 0, 1, 1, 1)

        self.target_entry = Gtk.Entry()
        self.target_entry.set_placeholder_text("Example: Linux Mint")
        self.target_entry.set_hexpand(True)
        self.target_entry.connect("activate", self._on_save)
        grid.attach(self.target_entry, 1, 1, 1, 1)

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

    def show(self) -> None:
        self.source_entry.set_text("")
        self.target_entry.set_text("")
        self.message_label.set_text("")
        self.window.show_all()
        self.window.present()
        self.source_entry.grab_focus()

    def _set_error(self, message: str) -> None:
        self.message_label.set_markup(f'<span foreground="#b00020">{GLib.markup_escape_text(message)}</span>')

    def _on_save(self, _widget) -> None:
        source = self.source_entry.get_text().strip()
        target = self.target_entry.get_text().strip()
        if not source or not target:
            self._set_error("Vul zowel het bronwoord als de vervanging in.")
            return
        try:
            action = self.app.add_word_replacement(source, target)
        except Exception as exc:
            logging.exception("Failed to save word replacement")
            self._set_error(str(exc))
            return

        notify(APP_NAME, f"Word replacement {action}: {source} -> {target}")
        self.window.hide()

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
        self.quick_add_replacement_window = QuickAddReplacementWindow(app, self.settings_window.window)
        self.about_window = self._build_about_window()
        self.menu = None
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

    def show_quick_add_replacement(self) -> None:
        GLib.idle_add(self._show_quick_add_replacement_on_main)

    def _show_quick_add_replacement_on_main(self) -> bool:
        self.quick_add_replacement_window.show()
        return False

    def show_about(self) -> None:
        GLib.idle_add(self._show_about_on_main)

    def _build_about_window(self) -> Gtk.Window:
        window = Gtk.Window(title=f"About {APP_NAME}")
        window.set_default_size(520, 340)
        window.set_border_width(16)
        window.connect("delete-event", self._hide_about_window)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        window.add(outer)

        title_label = Gtk.Label()
        title_label.set_markup(f'<span size="x-large" weight="bold">{APP_NAME}</span>')
        title_label.set_xalign(0)
        outer.pack_start(title_label, False, False, 0)

        version_label = Gtk.Label(label="Version 0.1.1")
        version_label.set_xalign(0)
        outer.pack_start(version_label, False, False, 0)

        description_label = Gtk.Label(
            label=(
                "Mint Dictate lets you dictate into text fields on Linux Mint and transcribes "
                "your speech through OpenAI or local Parakeet speech-to-text."
            )
        )
        description_label.set_xalign(0)
        description_label.set_line_wrap(True)
        outer.pack_start(description_label, False, False, 0)

        support_label = Gtk.Label(label=APP_SUPPORT_TEXT)
        support_label.set_xalign(0)
        support_label.set_line_wrap(True)
        outer.pack_start(support_label, False, False, 0)

        author_label = Gtk.Label(label=f"Made by {APP_AUTHOR}")
        author_label.set_xalign(0)
        outer.pack_start(author_label, False, False, 0)

        donation_button = Gtk.LinkButton.new_with_label(APP_DONATION_URL, "Support Mint Dictate on Ko-fi")
        donation_button.set_halign(Gtk.Align.START)
        outer.pack_start(donation_button, False, False, 0)

        link_button = Gtk.LinkButton.new_with_label(APP_WEBSITE, "Project on GitHub")
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

    def _build_model_backend_item(self, backend: str, label: str) -> Gtk.MenuItem:
        current_backend = self.app.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"])
        current_model = self.app.config.get("transcription_model", DEFAULT_CONFIG["transcription_model"])
        backend_item = Gtk.MenuItem(label=label)
        backend_menu = Gtk.Menu()
        for model_name in models_for_backend(backend):
            item = Gtk.CheckMenuItem(label=model_label(model_name))
            item.set_draw_as_radio(True)
            item.set_active(current_backend == backend and current_model == model_name)
            item.connect("activate", self._on_model_selected, backend, model_name)
            backend_menu.append(item)
        backend_item.set_submenu(backend_menu)
        return backend_item

    def _build_language_submenu(self) -> Gtk.Menu:
        current_language = self.app.config.get("language")
        menu = Gtk.Menu()

        auto_item = Gtk.CheckMenuItem(label="Auto Detect")
        auto_item.set_draw_as_radio(True)
        auto_item.set_active(current_language is None)
        auto_item.connect("activate", self._on_language_selected, None)
        menu.append(auto_item)

        for language_code in ("nl", "en"):
            item = Gtk.CheckMenuItem(label=language_label(language_code))
            item.set_draw_as_radio(True)
            item.set_active(current_language == language_code)
            item.connect("activate", self._on_language_selected, language_code)
            menu.append(item)

        extra_codes = [code for code, _label in LANGUAGE_OPTIONS if code not in {"nl", "en"}]
        if extra_codes:
            menu.append(Gtk.SeparatorMenuItem())
            more_item = Gtk.MenuItem(label="More Languages")
            more_menu = Gtk.Menu()
            for language_code in extra_codes:
                item = Gtk.CheckMenuItem(label=language_label(language_code))
                item.set_draw_as_radio(True)
                item.set_active(current_language == language_code)
                item.connect("activate", self._on_language_selected, language_code)
                more_menu.append(item)
            if current_language and current_language not in extra_codes and current_language not in {"nl", "en"}:
                more_menu.append(Gtk.SeparatorMenuItem())
                custom_item = Gtk.CheckMenuItem(label=f"Custom: {current_language}")
                custom_item.set_draw_as_radio(True)
                custom_item.set_active(True)
                custom_item.set_sensitive(False)
                more_menu.append(custom_item)
            more_item.set_submenu(more_menu)
            menu.append(more_item)
        return menu

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()

        toggle_item = Gtk.MenuItem(label="Stop Recording" if self.app.is_recording else "Start Recording")
        toggle_item.connect("activate", self._activate, self.app.toggle_recording)
        menu.append(toggle_item)

        if self.app.is_recording:
            cancel_item = Gtk.MenuItem(label="Cancel Recording")
            cancel_item.connect("activate", self._activate, self.app.cancel_recording)
            menu.append(cancel_item)

        status_item = Gtk.MenuItem(label=f"Status: {self.app.status_label()}")
        status_item.set_sensitive(False)
        menu.append(status_item)

        if self.app.should_show_local_progress():
            progress_item = Gtk.MenuItem(label=f"Local progress: {self.app.local_progress_percent()}%")
            progress_item.set_sensitive(False)
            menu.append(progress_item)

        local_worker_item = Gtk.MenuItem(label=self.app.local_worker_status_label())
        local_worker_item.set_sensitive(False)
        menu.append(local_worker_item)

        load_local_item = Gtk.MenuItem(label="Load Local Model")
        load_local_item.set_sensitive(
            self.app.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]) == "local"
            and not self.app.is_local_worker_loaded()
        )
        load_local_item.connect("activate", self._activate, self.app.load_local_model_now)
        menu.append(load_local_item)

        unload_local_item = Gtk.MenuItem(label="Unload Local Model")
        unload_local_item.set_sensitive(self.app.is_local_worker_loaded())
        unload_local_item.connect("activate", self._activate, self.app.unload_local_model_now)
        menu.append(unload_local_item)

        transcript = self.app.last_transcript.strip()
        preview = transcript[:40] + ("..." if len(transcript) > 40 else "")
        transcript_item = Gtk.MenuItem(label=f"Last Transcript: {preview or 'None'}")
        transcript_item.set_sensitive(False)
        menu.append(transcript_item)

        copy_item = Gtk.MenuItem(label="Copy Last Transcript Again")
        copy_item.set_sensitive(bool(transcript))
        copy_item.connect("activate", self._activate, self.app.copy_last_transcript)
        menu.append(copy_item)

        menu.append(self._build_model_backend_item("openai", "OpenAI model"))
        menu.append(self._build_model_backend_item("local", "Local model"))

        language_item = Gtk.MenuItem(label=f"Language: {language_label(self.app.config.get('language'))}")
        language_item.set_submenu(self._build_language_submenu())
        menu.append(language_item)

        add_replacement_item = Gtk.MenuItem(label="Add Word Replacement")
        add_replacement_item.connect("activate", self._activate, self.show_quick_add_replacement)
        menu.append(add_replacement_item)

        settings_item = Gtk.MenuItem(label="Settings")
        settings_item.connect("activate", self._activate, self.show_settings)
        menu.append(settings_item)

        donate_item = Gtk.MenuItem(label="Donate")
        donate_item.connect("activate", self._activate, self.app.open_donation_url)
        menu.append(donate_item)

        about_item = Gtk.MenuItem(label="About")
        about_item.connect("activate", self._activate, self.show_about)
        menu.append(about_item)

        restart_item = Gtk.MenuItem(label="Restart Mint Dictate")
        restart_item.connect("activate", self._activate, self.app.restart_service)
        menu.append(restart_item)

        menu.show_all()
        return menu

    def _on_model_selected(self, widget, backend: str, model_name: str) -> None:
        if widget.get_active():
            self.app.set_transcription_choice(backend, model_name)

    def _on_language_selected(self, widget, language_code: str | None) -> None:
        if widget.get_active():
            self.app.set_language_choice(language_code)

    def _refresh_on_main(self) -> bool:
        self.indicator.set_icon_full(self.icon_paths[self.app.state], self.app.status_label())
        self.indicator.set_title(f"{APP_NAME} ({self.app.status_label()})")
        if self.menu is not None:
            self.menu.destroy()
        self.menu = self._build_menu()
        self.indicator.set_menu(self.menu)
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

    def show_quick_add_replacement(self) -> None:
        self.app.show_add_word_replacement()

    def _build_menu(self):
        if not self.menu_supported:
            return None
        current_language = self.app.config.get("language")
        return pystray.Menu(
            pystray.MenuItem(
                lambda item: "Stop Recording" if self.app.is_recording else "Start Recording",
                lambda icon, item: self.app.toggle_recording(),
            ),
            pystray.MenuItem(
                "Cancel Recording",
                lambda icon, item: self.app.cancel_recording(),
                visible=lambda item: self.app.is_recording,
            ),
            pystray.MenuItem(
                lambda item: f"Status: {self.app.status_label()}",
                lambda icon, item: None,
                enabled=False,
            ),
            pystray.MenuItem(
                lambda item: f"Local progress: {self.app.local_progress_percent()}%",
                lambda icon, item: None,
                enabled=False,
                visible=lambda item: self.app.should_show_local_progress(),
            ),
            pystray.MenuItem(
                lambda item: self.app.local_worker_status_label(),
                lambda icon, item: None,
                enabled=False,
            ),
            pystray.MenuItem(
                "Load Local Model",
                lambda icon, item: self.app.load_local_model_now(),
                enabled=lambda item: (
                    self.app.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]) == "local"
                    and not self.app.is_local_worker_loaded()
                ),
            ),
            pystray.MenuItem(
                "Unload Local Model",
                lambda icon, item: self.app.unload_local_model_now(),
                enabled=lambda item: self.app.is_local_worker_loaded(),
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
            pystray.MenuItem(
                "OpenAI model",
                pystray.Menu(
                    *[
                        pystray.MenuItem(
                            model_label(model_name),
                            lambda icon, item, model=model_name: self.app.set_transcription_choice("openai", model),
                            checked=lambda item, model=model_name: (
                                self.app.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]) == "openai"
                                and self.app.config.get("transcription_model", DEFAULT_CONFIG["transcription_model"]) == model
                            ),
                        )
                        for model_name in OPENAI_TRANSCRIPTION_MODELS
                    ]
                ),
            ),
            pystray.MenuItem(
                "Local model",
                pystray.Menu(
                    *[
                        pystray.MenuItem(
                            model_label(model_name),
                            lambda icon, item, model=model_name: self.app.set_transcription_choice("local", model),
                            checked=lambda item, model=model_name: (
                                self.app.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]) == "local"
                                and self.app.config.get("transcription_model", DEFAULT_CONFIG["transcription_model"]) == model
                            ),
                        )
                        for model_name in LOCAL_TRANSCRIPTION_MODELS
                    ]
                ),
            ),
            pystray.MenuItem(
                lambda item: f"Language: {language_label(current_language)}",
                pystray.Menu(
                    pystray.MenuItem(
                        "Auto Detect",
                        lambda icon, item: self.app.set_language_choice(None),
                        checked=lambda item: self.app.config.get("language") is None,
                    ),
                    pystray.MenuItem(
                        "Dutch",
                        lambda icon, item: self.app.set_language_choice("nl"),
                        checked=lambda item: self.app.config.get("language") == "nl",
                    ),
                    pystray.MenuItem(
                        "English",
                        lambda icon, item: self.app.set_language_choice("en"),
                        checked=lambda item: self.app.config.get("language") == "en",
                    ),
                    pystray.MenuItem(
                        "More Languages",
                        pystray.Menu(
                            *[
                                pystray.MenuItem(
                                    label,
                                    lambda icon, item, code=code: self.app.set_language_choice(code),
                                    checked=lambda item, code=code: self.app.config.get("language") == code,
                                )
                                for code, label in LANGUAGE_OPTIONS
                                if code not in {"nl", "en"}
                            ]
                        ),
                    ),
                ),
            ),
            pystray.MenuItem("Add Word Replacement", lambda icon, item: self.show_quick_add_replacement()),
            pystray.MenuItem("Settings", lambda icon, item: self.show_settings()),
            pystray.MenuItem("Donate", lambda icon, item: self.app.open_donation_url()),
            pystray.MenuItem("About", lambda icon, item: self.app.show_about()),
            pystray.MenuItem("Restart Mint Dictate", lambda icon, item: self.app.restart_service()),
        )


class MintDictateApp:
    def __init__(self) -> None:
        self.config = load_config()
        self.client = None
        self.local_worker_process = None
        self.local_worker_profile = None
        self.local_worker_lock = threading.Lock()
        self.local_worker_unload_timer = None
        self._refresh_clients()
        self.state_lock = threading.Lock()
        self.audio_lock = threading.Lock()
        self.busy = False
        self.is_recording = False
        self.audio_chunks = []
        self.recording_started_at = 0.0
        self.transcription_started_at = 0.0
        self.recording_timer = None
        self.stream = None
        self.hotkey_listener = None
        self.state = "idle"
        self.last_error = ""
        self.last_transcript = ""
        self.local_progress = None
        self.local_progress_audio_duration = 0.0
        self.local_progress_timer = None
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
        self._cancel_local_progress_timer()
        self._cancel_local_worker_unload_timer()
        self._stop_local_worker()
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

    def _refresh_clients(self) -> None:
        backend = self.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"])
        if backend == "openai":
            api_key = self.config["openai_api_key"] or os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                raise RuntimeError("OpenAI API key ontbreekt in config.json of OPENAI_API_KEY.")
            timeout_seconds = float(
                self.config.get(
                    "openai_request_timeout_seconds",
                    DEFAULT_CONFIG["openai_request_timeout_seconds"],
                )
            )
            self.client = OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
        else:
            self.client = None
        self._cancel_local_worker_unload_timer()
        self._stop_local_worker()

    def apply_config(self, new_config: dict) -> None:
        self.config = DEFAULT_CONFIG.copy() | self.config | new_config
        self._refresh_clients()
        self._restart_hotkey_listener()
        self.ui.refresh()
        logging.info("Applied updated settings")

    def _persist_and_apply_config(self, updates: dict, success_message: str) -> None:
        new_config = dict(self.config)
        new_config.update(updates)
        save_user_config(new_config)
        self.apply_config(new_config)
        notify(APP_NAME, success_message)

    def set_transcription_choice(self, backend: str, model_name: str) -> None:
        if model_name not in models_for_backend(backend):
            notify(APP_NAME, f"Ongeldig model voor backend {backend}: {model_name}")
            return
        if backend == "openai":
            api_key = self.config.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                notify(APP_NAME, "OpenAI API key ontbreekt. Stel die eerst in via Settings of config.json.")
                return
        if backend == "local":
            dependency_error = local_model_dependency_error(model_name)
            if dependency_error:
                notify(APP_NAME, dependency_error)
                return
        try:
            self._persist_and_apply_config(
                {
                    "transcription_backend": backend,
                    "transcription_model": model_name,
                },
                f"Transcriptie ingesteld op {backend_label(backend)} / {model_label(model_name)}.",
            )
        except Exception as exc:
            logging.exception("Failed to switch transcription choice")
            notify(APP_NAME, f"Wisselen van transcriptiemodel mislukt: {exc}")

    def set_language_choice(self, language_code: str | None) -> None:
        try:
            self._persist_and_apply_config(
                {"language": language_code},
                f"Taal ingesteld op {language_label(language_code)}.",
            )
        except Exception as exc:
            logging.exception("Failed to switch language")
            notify(APP_NAME, f"Wisselen van taal mislukt: {exc}")

    def add_word_replacement(self, source: str, target: str) -> str:
        source = source.strip()
        target = target.strip()
        if not source or not target:
            raise ValueError("Elke vervangregel moet zowel bron als doel bevatten.")

        rules = parse_replacement_rules(replacement_rules_text(self.config))
        action = "added"
        updated_rules: list[tuple[str, str]] = []
        replaced_existing = False
        for existing_source, existing_target in rules:
            if existing_source == source:
                updated_rules.append((source, target))
                replaced_existing = True
                if existing_target != target:
                    action = "updated"
            else:
                updated_rules.append((existing_source, existing_target))
        if not replaced_existing:
            updated_rules.append((source, target))

        self._persist_and_apply_config(
            {"replacement_rules": format_replacement_rules(updated_rules)},
            f"Word replacement {action}: {source} -> {target}",
        )

        settings_window = getattr(self.ui, "settings_window", None)
        if settings_window is not None:
            settings_window.load_from_app_config()
        return action

    def open_config(self) -> None:
        config_path = USER_CONFIG_PATH if USER_CONFIG_PATH.exists() else PROJECT_CONFIG_PATH
        launch_path(config_path)

    def open_log(self) -> None:
        launch_path(LOG_PATH)

    def open_donation_url(self) -> None:
        launch_url(APP_DONATION_URL)

    def show_settings(self) -> None:
        self.ui.show_settings()

    def show_add_word_replacement(self) -> None:
        if hasattr(self.ui, "show_quick_add_replacement"):
            self.ui.show_quick_add_replacement()
            return
        notify(APP_NAME, "Quick add word replacement is not available on this tray backend.")

    def copy_last_transcript(self) -> None:
        if not self.last_transcript:
            return
        copy_to_clipboard(self.last_transcript)
        notify(APP_NAME, "Laatste transcript opnieuw naar clipboard gekopieerd.")

    def should_show_local_progress(self) -> bool:
        return (
            self.state == "transcribing"
            and self.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]) == "local"
            and self.local_progress is not None
        )

    def local_progress_percent(self) -> int:
        if self.local_progress is None:
            return 0
        return max(0, min(100, int(round(self.local_progress * 100))))

    def _set_local_progress(self, value: float | None) -> None:
        self.local_progress = value
        self.ui.refresh()

    def _current_local_stats_key(self) -> str:
        return "|".join(
            [
                self.config.get("transcription_model", DEFAULT_CONFIG["transcription_model"]),
                self.config.get("local_model_device", DEFAULT_CONFIG["local_model_device"]),
                self.config.get("local_model_compute_type", DEFAULT_CONFIG["local_model_compute_type"]),
            ]
        )

    def _expected_local_processing_seconds(self) -> float:
        duration = self.local_progress_audio_duration
        if duration <= 0:
            return 1.0

        stats = self.config.get("local_transcription_stats", {})
        profile = stats.get(self._current_local_stats_key(), {})
        avg_ratio = profile.get("avg_ratio")
        if isinstance(avg_ratio, (int, float)) and avg_ratio > 0:
            return max(duration * float(avg_ratio), 1.0)
        return max(duration * 1.5, 1.0)

    def _record_local_transcription_stats(self, duration: float, processing_seconds: float) -> None:
        if duration <= 0 or processing_seconds <= 0:
            return

        ratio = processing_seconds / duration
        ratio = max(0.05, min(ratio, 20.0))
        stats = dict(self.config.get("local_transcription_stats", {}))
        key = self._current_local_stats_key()
        current = stats.get(key, {})
        previous_avg = current.get("avg_ratio")
        previous_count = int(current.get("count", 0) or 0)

        if isinstance(previous_avg, (int, float)) and previous_avg > 0:
            avg_ratio = (float(previous_avg) * 0.7) + (ratio * 0.3)
        else:
            avg_ratio = ratio

        stats[key] = {
            "avg_ratio": round(avg_ratio, 4),
            "count": previous_count + 1,
            "last_ratio": round(ratio, 4),
        }
        self.config["local_transcription_stats"] = stats
        try:
            save_user_config(self.config)
        except Exception:
            logging.exception("Failed to persist local transcription stats")

    def _cancel_local_progress_timer(self) -> None:
        if self.local_progress_timer:
            self.local_progress_timer.cancel()
            self.local_progress_timer = None

    def _local_worker_config(self) -> tuple[str, str, str]:
        return (
            self.config["transcription_model"],
            self.config.get("local_model_device", DEFAULT_CONFIG["local_model_device"]),
            self.config.get("local_model_compute_type", DEFAULT_CONFIG["local_model_compute_type"]),
        )

    def _cancel_local_worker_unload_timer(self) -> None:
        if self.local_worker_unload_timer:
            self.local_worker_unload_timer.cancel()
            self.local_worker_unload_timer = None

    def is_local_worker_loaded(self) -> bool:
        with self.local_worker_lock:
            process = self.local_worker_process
            return process is not None and process.poll() is None

    def local_worker_status_label(self) -> str:
        if not self.is_local_worker_loaded():
            return "Local model: not loaded"
        profile = self.local_worker_profile
        model_name = profile[0] if profile else self.config.get("transcription_model", "")
        return f"Local model: loaded ({model_label(model_name)})"

    def load_local_model_now(self) -> None:
        if self.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]) != "local":
            notify(APP_NAME, "Kies eerst een lokaal model.")
            return
        if self.busy:
            notify(APP_NAME, "Mint Dictate is bezig. Probeer het zo opnieuw.")
            return
        try:
            self._ensure_local_worker()
            self._schedule_local_worker_unload()
            self.ui.refresh()
            notify(APP_NAME, f"Lokaal model geladen: {model_label(self.config['transcription_model'])}.")
        except Exception as exc:
            logging.exception("Failed to load local model")
            self.last_error = str(exc)
            self._set_state("error")
            notify(APP_NAME, f"Lokaal model laden mislukt: {exc}")

    def unload_local_model_now(self) -> None:
        self._cancel_local_worker_unload_timer()
        self._stop_local_worker()
        self.ui.refresh()
        notify(APP_NAME, "Lokaal model uit geheugen gehaald.")

    def _preload_local_worker_for_recording(self) -> None:
        if self.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]) != "local":
            return
        if self.is_local_worker_loaded():
            return
        try:
            self._ensure_local_worker()
            self.ui.refresh()
            logging.info("Preloaded local transcription worker during recording")
        except Exception as exc:
            logging.exception("Failed to preload local transcription worker during recording")
            self.last_error = str(exc)

    def _start_local_worker_preload_thread(self) -> None:
        if self.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]) != "local":
            return
        worker = threading.Thread(target=self._preload_local_worker_for_recording, daemon=True)
        worker.start()

    def _local_worker_idle_timeout_seconds(self) -> float:
        try:
            return float(
                self.config.get(
                    "local_model_idle_timeout_seconds",
                    DEFAULT_CONFIG["local_model_idle_timeout_seconds"],
                )
            )
        except (TypeError, ValueError):
            return float(DEFAULT_CONFIG["local_model_idle_timeout_seconds"])

    def _stop_local_worker(self) -> None:
        with self.local_worker_lock:
            process = self.local_worker_process
            self.local_worker_process = None
            self.local_worker_profile = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=1)
            except Exception:
                pass
        gc.collect()
        logging.info("Stopped local transcription worker")

    def _schedule_local_worker_unload(self) -> None:
        if self.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]) != "local":
            return
        with self.local_worker_lock:
            if self.local_worker_process is None or self.local_worker_process.poll() is not None:
                return
        idle_timeout_seconds = self._local_worker_idle_timeout_seconds()
        if idle_timeout_seconds <= 0:
            logging.info("Keeping local transcription worker loaded until app exit")
            return
        self._cancel_local_worker_unload_timer()
        self.local_worker_unload_timer = threading.Timer(
            idle_timeout_seconds,
            self._stop_local_worker_from_timer,
        )
        self.local_worker_unload_timer.daemon = True
        self.local_worker_unload_timer.start()
        logging.info(
            "Scheduled local transcription worker stop in %s seconds",
            idle_timeout_seconds,
        )

    def _stop_local_worker_from_timer(self) -> None:
        self.local_worker_unload_timer = None
        self._stop_local_worker()
        self.ui.refresh()

    def _ensure_local_worker(self):
        profile = self._local_worker_config()
        self._cancel_local_worker_unload_timer()
        with self.local_worker_lock:
            process = self.local_worker_process
            if (
                process is not None
                and process.poll() is None
                and self.local_worker_profile == profile
            ):
                return process

            if process is not None:
                try:
                    if process.stdin:
                        process.stdin.close()
                except Exception:
                    pass
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except Exception:
                    try:
                        process.kill()
                        process.wait(timeout=1)
                    except Exception:
                        pass

            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--local-transcribe-server",
                    profile[0],
                    profile[1],
                    profile[2],
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env_with_local_cuda_libraries() if profile[1] == "cuda" else None,
            )
            ready_line = process.stdout.readline() if process.stdout else ""
            try:
                ready_payload = json.loads(ready_line.strip()) if ready_line.strip() else {}
            except json.JSONDecodeError:
                ready_payload = {}
            if process.poll() is not None or not ready_payload.get("ready"):
                stderr_output = ""
                if process.stderr:
                    stderr_output = process.stderr.read().strip()
                worker_error = str(ready_payload.get("error", "")).strip()
                if worker_error:
                    logging.error("Local transcription worker startup failed: %s", worker_error)
                elif stderr_output:
                    logging.error("Local transcription worker startup stderr: %s", stderr_output)
                elif ready_line:
                    logging.error("Local transcription worker returned unexpected startup output: %s", ready_line.strip())
                self.local_worker_process = None
                self.local_worker_profile = None
                error_message = (
                    worker_error
                    or stderr_output
                    or "Lokale transcriptieworker startte niet correct. Bekijk ~/.cache/mint-dictate.log voor details."
                )
                raise RuntimeError(friendly_local_worker_error(error_message))

            self.local_worker_process = process
            self.local_worker_profile = profile
            logging.info("Started local transcription worker for %s/%s/%s", *profile)
            return process

    def _schedule_local_progress_tick(self) -> None:
        self._cancel_local_progress_timer()
        if not self.should_show_local_progress():
            return
        self.local_progress_timer = threading.Timer(0.25, self._local_progress_tick)
        self.local_progress_timer.daemon = True
        self.local_progress_timer.start()

    def _local_progress_tick(self) -> None:
        self.local_progress_timer = None
        if not self.should_show_local_progress():
            return
        expected_total = self._expected_local_processing_seconds()
        if self.transcription_started_at > 0:
            elapsed = max(0.0, time.time() - self.transcription_started_at)
            estimated = min(elapsed / expected_total, 0.95)
            if self.local_progress is None or estimated > self.local_progress:
                self.local_progress = estimated
                self.ui.refresh()
        self._schedule_local_progress_tick()

    def show_about(self) -> None:
        language_value = self.config.get("language") or "auto"
        message = (
            f"{APP_DESCRIPTION}\n"
            f"{APP_SUPPORT_TEXT}\n"
            f"Donate: {APP_DONATION_URL}\n"
            f"Made by {APP_AUTHOR}.\n"
            f"Hotkey: {self.config['hotkey']}\n"
            f"Backend: {self.config.get('transcription_backend', DEFAULT_CONFIG['transcription_backend'])}\n"
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

        stop_players = configured_stop_media_players(self.config)
        for player in self.paused_players:
            command = "stop" if player in stop_players else "pause"
            subprocess.run(
                [playerctl_path, "-p", player, command],
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

    def cancel_recording(self) -> None:
        with self.state_lock:
            if not self.is_recording:
                return

            self._cancel_timer()
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None

            with self.audio_lock:
                self.audio_chunks = []

            self.is_recording = False
            self.busy = False
            self.recording_started_at = 0.0
            self.transcription_started_at = 0.0
            self.local_progress_audio_duration = 0.0
            self._set_local_progress(None)
            self._resume_paused_media()
            self._set_state("idle")
            self.last_error = ""
            notify(APP_NAME, "Opname geannuleerd.")

    def _start_recording_internal(self) -> None:
        with self.audio_lock:
            self.audio_chunks = []
        self.local_progress = None
        self._cancel_local_worker_unload_timer()

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
        self._start_local_worker_preload_thread()
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
        self.transcription_started_at = time.time()
        self._set_local_progress(0.0 if self.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]) == "local" else None)
        if self.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]) == "local":
            self._schedule_local_progress_tick()
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
                self._cancel_local_progress_timer()
                self._set_local_progress(None)
                self.local_progress_audio_duration = 0.0
                self.transcription_started_at = 0.0
                self._set_state("idle")
                self.busy = False
                self._schedule_local_worker_unload()
                notify(APP_NAME, "Geen tekst herkend.")
                return

            logging.info("Transcription succeeded with %s characters", len(text))
            if self.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]) == "local":
                processing_seconds = max(0.0, time.time() - self.transcription_started_at)
                self._record_local_transcription_stats(self.local_progress_audio_duration, processing_seconds)
            self.last_transcript = text
            copy_to_clipboard(text)
            time.sleep(float(self.config["paste_delay_seconds"]))
            send_ctrl_v()

            self._cancel_local_progress_timer()
            self._set_local_progress(None)
            self.local_progress_audio_duration = 0.0
            self.transcription_started_at = 0.0
            self._set_state("idle")
            self.busy = False
            self._schedule_local_worker_unload()
            preview = text[:80] + ("..." if len(text) > 80 else "")
            notify(APP_NAME, f"Transcript geplakt: {preview}")
        except Exception as exc:
            logging.error("Transcription flow failed: %s", exc)
            logging.error(traceback.format_exc())
            self._cancel_local_progress_timer()
            self._set_local_progress(None)
            self.local_progress_audio_duration = 0.0
            self.transcription_started_at = 0.0
            self._set_state("error")
            self.busy = False
            self._schedule_local_worker_unload()
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
        if self.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]) == "local":
            text = self._transcribe_local(audio_path)
        else:
            text = self._transcribe_api(audio_path)
        return apply_configured_replacements(text, self.config)

    def _api_connectivity_target(self) -> tuple[str, int]:
        return OPENAI_CONNECTIVITY_HOST, OPENAI_CONNECTIVITY_PORT

    def _has_api_connectivity(self) -> bool:
        host, port = self._api_connectivity_target()
        try:
            with socket.create_connection(
                (host, port),
                timeout=2.0,
            ):
                return True
        except OSError:
            return False

    def _wait_for_api_connectivity(self, deadline: float) -> None:
        backend = self.config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"])
        if backend != "openai":
            return

        poll_seconds = max(
            1.0,
            float(
                self.config.get(
                    "openai_retry_poll_seconds",
                    DEFAULT_CONFIG["openai_retry_poll_seconds"],
                )
            ),
        )
        waiting_notified = False

        while time.time() < deadline:
            if self._has_api_connectivity():
                if waiting_notified:
                    self._set_state("transcribing")
                    self.last_error = ""
                    notify(APP_NAME, "Internetverbinding hersteld. Transcriptie wordt hervat.")
                return

            self.last_error = "Geen internetverbinding. Wachten op herstel om transcriptie te hervatten."
            self._set_state("waiting_network")
            if not waiting_notified:
                notify(
                    APP_NAME,
                    "Internetverbinding verbroken. Ik blijf wachten en probeer opnieuw zodra de verbinding terug is.",
                )
                waiting_notified = True
            time.sleep(poll_seconds)

        raise ApiNetworkUnavailableError(
            "Geen internetverbinding. Transcriptie hervat niet binnen de ingestelde wachttijd."
        )

    def _transcribe_api(self, audio_path: str) -> str:
        retry_window_seconds = max(
            5.0,
            float(
                self.config.get(
                    "openai_retry_window_seconds",
                    DEFAULT_CONFIG["openai_retry_window_seconds"],
                )
            ),
        )
        deadline = time.time() + retry_window_seconds
        request = {
            "model": self.config["transcription_model"],
            "language": self.config.get("language") or None,
        }
        cleaned_request = {key: value for key, value in request.items() if value is not None}

        while True:
            self._wait_for_api_connectivity(deadline)
            self._set_state("transcribing")
            try:
                with open(audio_path, "rb") as fh:
                    transcript = self.client.audio.transcriptions.create(
                        file=fh,
                        timeout=float(
                            self.config.get(
                                "openai_request_timeout_seconds",
                                DEFAULT_CONFIG["openai_request_timeout_seconds"],
                            )
                        ),
                        **cleaned_request,
                    )
                self.last_error = ""
                return transcript.text
            except (APIConnectionError, APITimeoutError) as exc:
                logging.warning("API transcription retry due to connectivity issue: %s", exc)
                self.last_error = f"Netwerkfout tijdens transcriptie: {exc}"
                if time.time() >= deadline:
                    raise ApiNetworkUnavailableError(
                        "Internetverbinding niet op tijd hersteld. Probeer opnieuw zodra je weer online bent."
                    ) from exc
            except APIStatusError as exc:
                if exc.status_code and int(exc.status_code) >= 500 and time.time() < deadline:
                    logging.warning("Retrying API transcription after server error %s", exc.status_code)
                    self.last_error = f"API tijdelijk niet bereikbaar ({exc.status_code}). Nieuwe poging volgt."
                    self._set_state("waiting_network")
                    time.sleep(
                        max(
                            1.0,
                            float(
                                self.config.get(
                                    "openai_retry_poll_seconds",
                                    DEFAULT_CONFIG["openai_retry_poll_seconds"],
                                )
                            ),
                        )
                    )
                    continue
                raise

    def _transcribe_local(self, audio_path: str) -> str:
        audio_info = sf.info(audio_path)
        duration = max(float(audio_info.duration or 0.0), 0.0)
        self.local_progress_audio_duration = duration
        process = self._ensure_local_worker()
        request = {
            "audio_path": audio_path,
            "language": self.config.get("language") or "",
        }
        if not process.stdin or not process.stdout:
            raise RuntimeError("Lokale transcriptieworker heeft geen geldige IO-kanalen.")
        try:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            response_line = process.stdout.readline()
        except Exception as exc:
            self._stop_local_worker()
            raise RuntimeError(f"Communicatie met lokale transcriptieworker mislukt: {exc}") from exc

        if process.poll() is not None and not response_line:
            stderr_output = process.stderr.read().strip() if process.stderr else ""
            self._stop_local_worker()
            raise RuntimeError(stderr_output or "Lokale transcriptieworker stopte onverwacht.")

        try:
            payload = json.loads(response_line.strip()) if response_line.strip() else {}
        except json.JSONDecodeError as exc:
            self._stop_local_worker()
            raise RuntimeError("Lokale transcriptieworker gaf ongeldige JSON terug.") from exc

        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        self._set_local_progress(1.0)
        return str(payload.get("text") or "").strip()

    def _set_state(self, state: str) -> None:
        self.state = state
        self.ui.refresh()

    def status_label(self) -> str:
        labels = {
            "idle": "Idle",
            "recording": "Recording",
            "transcribing": "Transcribing",
            "waiting_network": "Offline - waiting to retry",
            "error": "Error",
        }
        return labels.get(self.state, self.state.title())

    def tooltip_text(self) -> str:
        lines = [APP_NAME, f"Status: {self.status_label()}", f"Hotkey: {self.config['hotkey']}"]
        if self.should_show_local_progress():
            lines.append(f"Local progress: {self.local_progress_percent()}%")
        if self.last_error:
            lines.append(f"Last error: {self.last_error[:60]}")
        elif self.last_transcript:
            lines.append(f"Last transcript: {self.last_transcript[:60]}")
        return "\n".join(lines)


def main() -> None:
    if len(sys.argv) >= 5 and sys.argv[1] == "--local-transcribe-server":
        raise SystemExit(
            run_local_transcription_worker_server(
                model_name=sys.argv[2],
                device=sys.argv[3],
                compute_type=sys.argv[4],
            )
        )

    app = MintDictateApp()

    def handle_signal(signum, frame) -> None:
        app.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    app.run()


if __name__ == "__main__":
    main()
