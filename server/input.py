"""Эмуляция ввода: мышь, клавиатура, Unicode-текст через WinAPI SendInput."""
import ctypes
import json
from typing import List, Tuple
from .config import IS_WINDOWS
from .winapi import (
    _user32,
    INPUT,
    INPUT_MOUSE,
    INPUT_KEYBOARD,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_MIDDLEDOWN,
    MOUSEEVENTF_MIDDLEUP,
    MOUSEEVENTF_WHEEL,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_ABSOLUTE,
)
if IS_WINDOWS:
    from .winapi import MOUSEINPUT, KEYBDINPUT, _INPUT_UNION

__all__ = [
    "_send_inputs",
    "_vk_input",
    "_key_input",
    "_unicode_inputs",
    "_mouse_input",
    "_abs_coords",
    "handle_mouse",
    "handle_type_text",
]


def _send_inputs(inputs: list) -> None:
    if not IS_WINDOWS or not inputs:
        return
    arr = (INPUT * len(inputs))(*inputs)
    _user32.SendInput(len(inputs), ctypes.cast(arr, ctypes.c_void_p), ctypes.sizeof(INPUT))


def _vk_input(vk: int, key_up: bool = False) -> "INPUT":
    flags = KEYEVENTF_KEYUP if key_up else 0
    return INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUT_UNION(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)),
    )


def _key_input(vk: int, flags: int = 0) -> "INPUT":
    """Универсальная эмуляция нажатия/отпускания клавиши по VK-коду и флагам (для UDP-геймпада)."""
    return INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUT_UNION(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)),
    )


def _unicode_inputs(text: str) -> list:
    """Формирует события нажатия символов в UTF-16 (не зависит от текущей раскладки клавиатуры ПК)."""
    events = []
    utf16_bytes = text.encode("utf-16-le")
    for i in range(0, len(utf16_bytes), 2):
        code_unit = int.from_bytes(utf16_bytes[i : i + 2], "little")
        events.append(
            INPUT(
                type=INPUT_KEYBOARD,
                u=_INPUT_UNION(
                    ki=KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=0)
                ),
            )
        )
        events.append(
            INPUT(
                type=INPUT_KEYBOARD,
                u=_INPUT_UNION(
                    ki=KEYBDINPUT(
                        wVk=0,
                        wScan=code_unit,
                        dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                        time=0,
                        dwExtraInfo=0,
                    )
                ),
            )
        )
    return events


def _mouse_input(dx: int = 0, dy: int = 0, data: int = 0, flags: int = 0) -> "INPUT":
    return INPUT(
        type=INPUT_MOUSE,
        u=_INPUT_UNION(
            mi=MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0)
        ),
    )


def _abs_coords(data: dict) -> Tuple[int, int]:
    nx = max(0.0, min(1.0, float(data.get("nx", 0.5))))
    ny = max(0.0, min(1.0, float(data.get("ny", 0.5))))
    return int(nx * 65535), int(ny * 65535)


def handle_mouse(body: str) -> Tuple[int, str]:
    if not IS_WINDOWS:
        return 400, "мышь поддерживается только на Windows"
    try:
        data = json.loads(body) if body.startswith("{") else {}
    except Exception:
        return 400, "некорректный JSON"

    action = str(data.get("action", "move"))
    all_mons = bool(data.get("allMonitors", False))
    try:
        if action == "move":
            dx = max(-2000, min(2000, int(data.get("dx", 0))))
            dy = max(-2000, min(2000, int(data.get("dy", 0))))
            if dx or dy:
                _send_inputs([_mouse_input(dx=dx, dy=dy, flags=MOUSEEVENTF_MOVE)])
            return 200, "ok"

        if action in ("abs_move", "abs_click", "abs_double", "abs_right"):
            ax, ay = _abs_coords(data)
            move_flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
            if all_mons:
                move_flags |= MOUSEEVENTF_VIRTUALDESK
            events = [_mouse_input(dx=ax, dy=ay, flags=move_flags)]
            if action == "abs_click":
                events += [
                    _mouse_input(flags=MOUSEEVENTF_LEFTDOWN),
                    _mouse_input(flags=MOUSEEVENTF_LEFTUP),
                ]
            elif action == "abs_double":
                click = [
                    _mouse_input(flags=MOUSEEVENTF_LEFTDOWN),
                    _mouse_input(flags=MOUSEEVENTF_LEFTUP),
                ]
                events += click + click
            elif action == "abs_right":
                events += [
                    _mouse_input(flags=MOUSEEVENTF_RIGHTDOWN),
                    _mouse_input(flags=MOUSEEVENTF_RIGHTUP),
                ]
            _send_inputs(events)
            return 200, "ok"

        if action == "left":
            _send_inputs(
                [
                    _mouse_input(flags=MOUSEEVENTF_LEFTDOWN),
                    _mouse_input(flags=MOUSEEVENTF_LEFTUP),
                ]
            )
            return 200, "ok"

        if action == "double":
            click = [
                _mouse_input(flags=MOUSEEVENTF_LEFTDOWN),
                _mouse_input(flags=MOUSEEVENTF_LEFTUP),
            ]
            _send_inputs(click + click)
            return 200, "ok"

        if action == "right":
            _send_inputs(
                [
                    _mouse_input(flags=MOUSEEVENTF_RIGHTDOWN),
                    _mouse_input(flags=MOUSEEVENTF_RIGHTUP),
                ]
            )
            return 200, "ok"

        if action == "middle":
            _send_inputs(
                [
                    _mouse_input(flags=MOUSEEVENTF_MIDDLEDOWN),
                    _mouse_input(flags=MOUSEEVENTF_MIDDLEUP),
                ]
            )
            return 200, "ok"

        if action == "scroll":
            steps = max(-20, min(20, int(data.get("dy", 0))))
            if steps:
                _send_inputs([_mouse_input(data=steps * 120, flags=MOUSEEVENTF_WHEEL)])
            return 200, "ok"

        return 404, "неизвестное действие мыши"
    except Exception as e:
        return 500, f"ошибка мыши: {e}"


def handle_type_text(body: str) -> Tuple[int, str]:
    if not IS_WINDOWS:
        return 400, "ввод текста поддерживается только на Windows"
    text = body
    press_enter = False
    if body.startswith("{"):
        try:
            data = json.loads(body)
            text = str(data.get("text", ""))
            press_enter = bool(data.get("enter", False))
        except Exception:
            pass
    if not text and not press_enter:
        return 400, "пустой текст"
    if len(text) > 1000:
        text = text[:1000]
    try:
        events = _unicode_inputs(text)
        if press_enter:
            events.append(_vk_input(0x0D, False))
            events.append(_vk_input(0x0D, True))
        _send_inputs(events)
        return 200, "ok"
    except Exception as e:
        return 500, f"ошибка ввода текста: {e}"
