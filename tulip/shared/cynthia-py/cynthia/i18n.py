# cynthia/i18n.py
# UI string table for CYNTHIA's on-screen interface.
#
# Only *chrome* is translated: headers, menu titles, buttons, settings labels
# and the knob parameter names. Instrument names (from meta.json) and patch
# names (user-typed) are NEVER translated — they are content, and a Russian
# user who names a patch "Bass" should still see "Bass".
#
# Cyrillic rendering: the U8G2 fonts shipped in cynthia_fonts.h are ASCII-only
# (_tr = "transparent, restricted"), so a Cyrillic string would draw as two
# garbage glyphs per character. cynthia_fonts_cyr.h supplies the same faces
# with Cyrillic coverage; cynthia_ui_draw_str decodes UTF-8 and picks the
# Cyrillic variant when a non-ASCII codepoint appears.
#
# Keep every Russian string SHORT: the OLED is 128px wide and the widest font
# used for labels (FONT_5X7) fits ~21 characters. Where a literal translation
# would overflow, the table carries the abbreviation a Russian speaker would
# actually expect on a synth display.

import json

SETTINGS_PATH = "/user/settings.json"

# Available locales. The first entry is the fallback.
LOCALES = ("en", "ru")
DEFAULT_LOCALE = "en"

# English is the key language: every string used by the UI must appear here so
# a missing translation is a visible fallback (the English text) rather than a
# KeyError or an empty label.
_EN = {
    # --- header / status ---
    "oct": "OCT",                    # OCT:+1
    # --- instrument grid ---
    "instruments": "INSTRUMENTS",
    "instruments_menu": "CYNTHIA instruments:",
    "settings": "SETTINGS",
    "empty": "<Empty>",
    # --- save-patch menu ---
    "save_patch": "SAVE PATCH",
    "save": "Save",
    "cancel": "Cancel",
    "patch_name": "Name",
    # --- patch list ---
    "user": "[User]",
    "default": "[Default]",
    # --- knob parameter names (§standard pot mapping) ---
    "custom1": "Custom 1", "custom2": "Custom 2",
    "custom3": "Custom 3", "custom4": "Custom 4",
    "rate": "Rate",
    "depth": "Depth",
    "target": "Target",
    "cutoff": "Cutoff",
    "resonance": "Resonance",
    "attack": "Attack",
    "decay": "Decay",
    "sustain": "Sustain",
    "release": "Release",
    "delay": "Delay",
    "delay_t": "Delay T",
    "glide": "Glide",
    # --- settings menu ---
    "settings_title": "SETTINGS",
    "language": "Language",
    "midi_channel": "MIDI Ch",
    "velocity": "Velocity",
    "octave": "Octave",
    "ble_midi": "BLE MIDI",
    "ble_name": "BLE Name",
    "ble_on": "On",
    "ble_off": "Off",
    "back": "Back",
    "apply_reboot": "Reboot to apply",
    "ble_status": "BLE Status",
    "connected": "Connected",
    "advertising": "Advertising",
    "ble_error": "BLE error",
    # --- misc / diagnostics ---
    "loading": "Loading",
    "ready": "ready",
    "none": "---",
    "no_instrument": "No instrument",
    "calibrate": "CALIBRATE",
    "cal_hold_still": "Hold stick still",
    "cal_start_hint": "Starting...",
    "cal_sweep": "Sweep stick around",
    "cal_test": "Test - move stick",
    "cal_failed": "Failed: no samples",
    "cal_retry": "Try again",
    "cal_save_failed": "Could not save",
    "cancel_prompt": "cancel",
    "loading_prompt": "Loading",
}

_RU = {
    # --- header / status ---
    "oct": "ОКТ",
    # --- instrument grid ---
    "instruments": "ИНСТРУМЕНТЫ",
    "instruments_menu": "CYNTHIA — инструменты:",
    "settings": "НАСТРОЙКИ",
    "empty": "<Пусто>",
    # --- save-patch menu ---
    "save_patch": "СОХР. ПАТЧ",
    "save": "Сохранить",
    "cancel": "Отмена",
    "patch_name": "Имя",
    # --- patch list ---
    "user": "[Свои]",
    "default": "[Завод]",
    # --- knob parameter names ---
    "custom1": "Свой 1", "custom2": "Свой 2",
    "custom3": "Свой 3", "custom4": "Свой 4",
    "rate": "Скорость",
    "depth": "Глубина",
    "target": "Цель",
    "cutoff": "Срез",
    "resonance": "Резонанс",
    "attack": "Атака",
    "decay": "Спад",
    "sustain": "Уровень",
    "release": "Затух.",
    "delay": "Эхо",
    "delay_t": "Эхо вр.",
    "glide": "Скольж.",
    # --- settings menu ---
    "settings_title": "НАСТРОЙКИ",
    "language": "Язык",
    "midi_channel": "MIDI кан.",
    "velocity": "Громк.",
    "octave": "Октава",
    "ble_midi": "BLE MIDI",
    "ble_name": "Имя BLE",
    "ble_on": "Вкл",
    "ble_off": "Выкл",
    "back": "Назад",
    "apply_reboot": "Перезагрузка",
    "ble_status": "Состояние BLE",
    "connected": "Подключено",
    "advertising": "Поиск",
    "ble_error": "Ошибка BLE",
    # --- misc / diagnostics ---
    "loading": "Загрузка",
    "ready": "готов",
    "none": "---",
    "no_instrument": "Нет инструмента",
    "calibrate": "КАЛИБРОВКА",
    "cal_hold_still": "Не трогайте стик",
    "cal_start_hint": "Запуск...",
    "cal_sweep": "Двигайте стик",
    "cal_test": "Проверка",
    "cal_failed": "Нет данных",
    "cal_retry": "Ещё раз",
    "cal_save_failed": "Не сохранилось",
    "cancel_prompt": "отмена",
    "loading_prompt": "Загрузка",
}

_TABLES = {"en": _EN, "ru": _RU}

_locale = DEFAULT_LOCALE


def _load_locale():
    """Read the persisted locale from settings.json (best effort)."""
    global _locale
    try:
        with open(SETTINGS_PATH) as f:
            loc = json.load(f).get("locale")
    except (OSError, ValueError):
        return
    if loc in LOCALES:
        _locale = loc


def locale(code=None):
    """Get or set the active locale. Setting persists it to settings.json.

    Returns the active code. An unknown code is ignored (stays as-is) rather
    than raising: this runs from the settings menu, where a bad value should
    never take the UI down.
    """
    global _locale
    if code is not None:
        if code not in LOCALES:
            return _locale
        if code != _locale:
            _locale = code
            _persist(code)
    return _locale


def _persist(code):
    """Write just the locale field, preserving everything else in settings.json."""
    data = {}
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        pass
    data["locale"] = code
    try:
        with open(SETTINGS_PATH, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def t(key):
    """Translate `key` into the active locale.

    Falls back to English, then to the key itself, so a typo shows up on screen
    as the offending key rather than as nothing at all.
    """
    table = _TABLES.get(_locale)
    if table is not None and key in table:
        return table[key]
    if key in _EN:
        return _EN[key]
    return key


def is_cyrillic(text):
    """True if `text` contains a non-ASCII character (i.e. needs a Cyrillic font)."""
    for ch in text:
        if ord(ch) > 127:
            return True
    return False


# Load the persisted choice at import time: the boot path builds the UI
# immediately, so the locale must already be correct when t() is first called.
_load_locale()
