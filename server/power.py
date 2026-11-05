"""Управление питанием Windows: выключение, перезагрузка, сон, таймеры, гашение экрана."""
import subprocess
import threading
import time
from typing import Dict, Tuple, List, Optional, Any

from .config import IS_WINDOWS, CREATE_NO_WINDOW, log
from .winapi import _user32, _powrprof

__all__ = [
    "POWER",
    "turn_off_monitor",
    "sleep_pc",
    "set_custom_timer",
    "run_power",
    "get_shutdown_remaining_sec",
]

# Команды управления питанием и сессией (команда subprocess, таймер в сек или None)
POWER: Dict[str, Tuple[List[str], Optional[int]]] = {
    "/shutdown": (["shutdown", "/s", "/t", "3"], 3),
    "/shutdown_15m": (["shutdown", "/s", "/t", "900"], 900),
    "/shutdown_30m": (["shutdown", "/s", "/t", "1800"], 1800),
    "/shutdown_60m": (["shutdown", "/s", "/t", "3600"], 3600),
    "/shutdown_120m": (["shutdown", "/s", "/t", "7200"], 7200),
    "/restart": (["shutdown", "/r", "/t", "3"], 3),
    "/cancel": (["shutdown", "/a"], 0),
    "/lock": (["rundll32.exe", "user32.dll,LockWorkStation"], None),
}

_shutdown_deadline: float = 0.0
_timer_generation: int = 0


def get_shutdown_remaining_sec() -> int:
    global _shutdown_deadline
    now = time.time()
    return max(0, int(_shutdown_deadline - now)) if _shutdown_deadline > now else 0


def turn_off_monitor() -> Tuple[int, str]:
    if not IS_WINDOWS:
        return 400, "только для Windows"
    try:
        _user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)
        return 200, "ok"
    except Exception as e:
        return 500, f"не удалось погасить экран: {e}"


def sleep_pc() -> None:
    if not IS_WINDOWS:
        return
    try:
        time.sleep(0.3)
        _powrprof.SetSuspendState(0, 0, 0)
    except Exception as e:
        log(f"[!] Не удалось перевести ПК в сон: {e}")


def set_custom_timer(minutes: int, action: str) -> Tuple[int, str]:
    """Устанавливает таймер выключения или сна на произвольное количество минут с защитой от гонки потоков."""
    global _shutdown_deadline, _timer_generation
    if minutes <= 0:
        return 400, "время должно быть больше 0 минут"
    sec = minutes * 60
    _shutdown_deadline = time.time() + sec
    _timer_generation += 1
    current_gen = _timer_generation

    if action == "sleep":
        subprocess.run(["shutdown", "/a"], capture_output=True, timeout=5, creationflags=CREATE_NO_WINDOW)

        def worker(gen_id: int, deadline: float) -> None:
            global _shutdown_deadline, _timer_generation
            while time.time() < deadline:
                time.sleep(1)
                if _timer_generation != gen_id or _shutdown_deadline <= 0:
                    return
            if _timer_generation == gen_id:
                sleep_pc()
                _shutdown_deadline = 0.0

        t = threading.Thread(target=worker, args=(current_gen, _shutdown_deadline), daemon=True)
        t.start()
        log(f"Установлен таймер сна на {minutes} мин ({sec} сек, gen #{current_gen})")
        return 200, f"Таймер сна на {minutes} мин запущен"
    else:
        run_power(["shutdown", "/s", "/t", str(sec)], sec)
        return 200, f"Таймер выключения на {minutes} мин запущен"


def run_power(cmd: List[str], timer_sec: Optional[int]) -> None:
    global _shutdown_deadline
    log(f"Выполняю: {' '.join(cmd)}")
    try:
        if cmd[0] == "shutdown" and len(cmd) > 1 and cmd[1] != "/a":
            subprocess.run(["shutdown", "/a"], capture_output=True, timeout=5, creationflags=CREATE_NO_WINDOW)
        subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
        if timer_sec == 0:
            _shutdown_deadline = 0.0
        elif isinstance(timer_sec, int) and timer_sec > 10:
            _shutdown_deadline = time.time() + timer_sec
    except Exception as e:
        log(f"[!] Не удалось выполнить команду питания: {e}")
