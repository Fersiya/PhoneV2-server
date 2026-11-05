# PhoneV2-server

Python-сервер (Windows 11) для приложения [PhoneV2](https://github.com/Fersiya/PhoneV2): управление ПК с телефона.

## Запуск

- `python server.py` — в консоли; `python server.py --init` — показать IP, порт и токен.
- `pythonw server.py` — в фоне (ошибки пишутся в `server.log`).
- `install_autostart.bat` (от администратора) — автозагрузка и правила брандмауэра (только локальная сеть и Tailscale).

Порт 8765 (TCP и UDP). Токен и настройки — в `config.json` (создаётся при первом запуске, в git не попадает).
Интернет-туннель Cloudflare стартует сам (чтобы телефон работал вне дома); отключить: `"tunnel_autostart": false` в `config.json`.
Обновления приложения: положи APK с GitHub Actions в папку `updates/`.

## Структура

Точка входа `server.py`, код в пакете `server/` (winapi, capture, input, handler, network, auth, ota и др.).
Тесты: `python -m unittest discover tests` (на Windows и Linux).

## Безопасность

Токен идёт открытым текстом по HTTP/UDP: вне домашней сети используй Tailscale или Cloudflare-туннель.
После 10 неверных токенов IP блокируется на 15 минут. Порт 8765 в интернет напрямую не пробрасывать.

## Синхронизация

`sync_github.bat` — commit → `git pull --rebase` → push, без force push.
