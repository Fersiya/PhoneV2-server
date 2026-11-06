"""Пульт ПК Хост — программа на компьютере (Windows 10/11), которой управляет телефон.

Запуск в консоли:       python server.py
Проверка и инфо:        python server.py --init
Скрытый запуск (фон):   pythonw server.py
Автоустановка:          install_autostart.bat (от имени администратора)

При первом запуске создаётся config.json с секретным токеном и списком программ.
Изменения в config.json подхватываются автоматически без перезапуска сервера.
В скрытом режиме ошибки и действия пишутся в server.log рядом с этим файлом.
"""
import sys

# Обеспечиваем корректный вывод UTF-8 в Windows-консоли (защита от UnicodeEncodeError на cp1251)
if sys.platform == "win32":
    try:
        if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from server import (
    PORT,
    TOKEN,
    log,
    check_paths,
    get_local_ips,
    start_network_services,
    start_tray_thread,
    Handler,
    PcServer,
)
from server.version import VERSION
from server.updates import start_github_updates


def main() -> None:
    if "--init" in sys.argv:
        ips = ", ".join(get_local_ips()) or "не определён"
        print(f"  IP-адреса ПК: {ips}")
        print(f"  Порт:         {PORT}")
        print(f"  Токен:        {TOKEN}")
        sys.exit(0)

    check_paths()
    local_ips = ", ".join(get_local_ips()) or "127.0.0.1"
    print("\n" + "=" * 70)
    print(f"  [*] ПУЛЬТ ПК ХОСТ {VERSION} — ЗАПУЩЕН И ГОТОВ К РАБОТЕ")
    print("=" * 70)
    print(f"  [+] СЕКРЕТНЫЙ ТОКЕН:  {TOKEN}")
    print(f"  [+] ЛОКАЛЬНЫЙ WI-FI:   http://{local_ips}:{PORT}")
    print("=" * 70 + "\n")
    log(f"Сервер {VERSION} слушает порт {PORT} (токен в логах не пишется)")

    import time
    import urllib.request

    # Занимаем порт первым делом: если Хост уже запущен — просим его показать окно и выходим
    server = None
    for attempt in range(6):
        try:
            server = PcServer(("0.0.0.0", PORT), Handler)
            break
        except OSError as e:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/show_window", timeout=2).read()
                log("[!] Хост уже запущен — показал его окно")
                raise SystemExit(0)
            except SystemExit:
                raise
            except Exception:
                if attempt == 5:
                    log(f"[!] Не удалось занять порт {PORT}: {e}")
                    raise SystemExit(1)
                time.sleep(1)  # возможно, прошлый экземпляр ещё закрывается (перезапуск)

    from server.auth import open_pairing
    from server.config import CFG
    if not CFG.get("paired"):
        open_pairing(900)  # первый запуск: 15 минут на подключение первого телефона
    start_network_services()
    start_tray_thread()
    start_github_updates()
    if "--minimized" not in sys.argv:
        from server import show_status_dialog
        show_status_dialog()  # при ручном запуске сразу показываем окно; автозапуск идёт тихо, в трей
    server.serve_forever()


if __name__ == "__main__":
    main()
