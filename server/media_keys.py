"""Управление мультимедиа, горячими клавишами и плером YouTube."""
import time
from typing import Tuple, Dict, List
from .config import IS_WINDOWS
from .input import _send_inputs, _vk_input
from .windows import focus_browser

__all__ = [
    "MEDIA_KEYS",
    "COMBO_KEYS",
    "BROWSER_ACTIONS",
    "send_media_key",
]

# Одиночные клавиши (код VK_*, количество нажатий)
MEDIA_KEYS: Dict[str, Tuple[int, int]] = {
    "vol_up": (0xAF, 2),          # VK_VOLUME_UP (~4% за вызов)
    "vol_down": (0xAE, 2),        # VK_VOLUME_DOWN (~4% за вызов)
    "vol_up_10": (0xAF, 5),       # VK_VOLUME_UP (~10% за вызов)
    "vol_down_10": (0xAE, 5),     # VK_VOLUME_DOWN (~10% за вызов)
    "vol_mute": (0xAD, 1),        # VK_VOLUME_MUTE
    "play_pause": (0xB3, 1),      # VK_MEDIA_PLAY_PAUSE (системная)
    "next": (0xB0, 1),            # VK_MEDIA_NEXT_TRACK
    "prev": (0xB1, 1),            # VK_MEDIA_PREV_TRACK
    "yt_play_k": (0x4B, 1),       # Клавиша 'K' (пауза/пуск конкретно в плеере YouTube)
    "yt_back": (0x25, 1),         # VK_LEFT (-5 сек в YouTube/плеере)
    "yt_forward": (0x27, 1),      # VK_RIGHT (+5 сек в YouTube/плеере)
    "yt_back_10": (0x4A, 1),      # Клавиша 'J' (-10 сек в YouTube)
    "yt_forward_10": (0x4C, 1),   # Клавиша 'L' (+10 сек в YouTube)
    "yt_fullscreen": (0x46, 1),   # Клавиша 'F' (полный экран в YouTube/плеере)
    "yt_subtitles": (0x43, 1),    # Клавиша 'C' (субтитры в YouTube)
    "yt_mute": (0x4D, 1),         # Клавиша 'M' (звук в плеере YouTube)
    "yt_theater": (0x54, 1),      # Клавиша 'T' (режим кинотеатра в YouTube)
    "yt_mini": (0x49, 1),         # Клавиша 'I' (мини-плеер в YouTube)
    "yt_up": (0x26, 1),           # VK_UP (предыдущий Shorts / прокрутка вверх)
    "yt_down": (0x28, 1),         # VK_DOWN (следующий Shorts / прокрутка вниз)
    "key_enter": (0x0D, 1),       # Enter
    "key_backspace": (0x08, 1),   # Backspace
    "key_esc": (0x1B, 1),         # Escape
    "key_space": (0x20, 1),       # Пробел
}

# Сочетания клавиш (список кодов VK_*, зажимаемых одновременно)
COMBO_KEYS: Dict[str, List[int]] = {
    "close_tab": [0x11, 0x57],         # Ctrl + W (закрыть текущую вкладку браузера)
    "reopen_tab": [0x11, 0x10, 0x54],  # Ctrl + Shift + T (вернуть закрытую вкладку)
    "next_tab": [0x11, 0x09],          # Ctrl + Tab (следующая вкладка)
    "win_d": [0x5B, 0x44],             # Win + D (свернуть/развернуть все окна)
    "alt_tab": [0x12, 0x09],           # Alt + Tab (переключить активное окно)
    "alt_f4": [0x12, 0x73],            # Alt + F4 (закрыть текущее окно)
    "audio_output": [0x5B, 0x11, 0x56],  # Win + Ctrl + V (меню выбора аудиовыхода в Windows 11)
    "task_mgr": [0x11, 0x10, 0x1B],      # Ctrl + Shift + Esc (Диспетчер задач)
    "win_tab": [0x5B, 0x09],             # Win + Tab (Представление задач)
    "ctrl_z": [0x11, 0x5A],              # Ctrl + Z (Отменить)
    "ctrl_c": [0x11, 0x43],              # Ctrl + C (Копировать)
    "ctrl_v": [0x11, 0x56],              # Ctrl + V (Вставить)
    "yt_speed_up": [0x10, 0xBE],         # Shift + . (> ускорить видео)
    "yt_speed_down": [0x10, 0xBC],       # Shift + , (< замедлить видео)
}

BROWSER_ACTIONS = {
    "yt_play_k",
    "yt_back",
    "yt_forward",
    "yt_back_10",
    "yt_forward_10",
    "yt_fullscreen",
    "yt_subtitles",
    "yt_mute",
    "yt_theater",
    "yt_mini",
    "yt_speed_up",
    "yt_speed_down",
    "yt_up",
    "yt_down",
    "close_tab",
    "reopen_tab",
    "next_tab",
}


def send_media_key(action: str) -> Tuple[int, str]:
    if not IS_WINDOWS:
        return 400, "клавиши поддерживаются только на Windows"
    if action not in MEDIA_KEYS and action not in COMBO_KEYS:
        return 404, "неизвестная команда"
    try:
        if action in BROWSER_ACTIONS:
            if not focus_browser():
                return 409, "Brave не открыт или не вышел на передний план. Открой видео кнопкой «На ПК»."
            time.sleep(0.08)

        if action in COMBO_KEYS:
            keys = COMBO_KEYS[action]
            events = [_vk_input(vk, False) for vk in keys] + [_vk_input(vk, True) for vk in reversed(keys)]
            _send_inputs(events)
            return 200, "ok"

        vk, repeats = MEDIA_KEYS[action]
        events = []
        for _ in range(repeats):
            events.append(_vk_input(vk, False))
            events.append(_vk_input(vk, True))
        _send_inputs(events)
        return 200, "ok"
    except Exception as e:
        return 500, f"ошибка отправки клавиши: {e}"
