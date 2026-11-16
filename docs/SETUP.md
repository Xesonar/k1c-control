# Установка K1C Control с нуля

Инструкция для своего сервера на Ubuntu 24.04. Можно использовать только Telegram или включить Telegram и закрытый веб-пульт вместе. Установка программы на сервер не запускает печать.

Все адреса `printer.local` и `dashboard.example.com` ниже — примеры. Замените их своими адресами. Токен, пароли, конфигурацию и базы не публикуйте в репозитории.
## 1. Что понадобится

- Совместимый Creality K1C с работающим Moonraker. Проверена ветка **K1C 2025 / V1.0.0.24**. Другую прошивку и её штатные команды нужно проверить отдельно.
- Компьютер или VPS с Ubuntu 24.04, Python 3.9+ и постоянным доступом к Telegram и принтеру. В Ubuntu 24.04 штатный Python подходит.
- Telegram-аккаунт владельца и отдельный бот, созданный через BotFather.
- Для сайта: домен либо публичный IP сервера, доступные порты 80/443 и HTTPS.
- Для записи видео: `ffmpeg`, `ffprobe`, доступный видеопоток и свободное место. Архив ограничен 10 GiB, но кольцевой буфер и временные файлы тоже занимают диск.
- Для STL: отдельно установленный совместимый OrcaSlicer. Для детектора: отдельно установленный Docker и Obico. Эти функции можно добавить позже.

Не перепрошивайте принтер по этой инструкции. Сначала настройте поддерживаемый для вашей модели Moonraker/Helper Script по документации соответствующего проекта. У K1C 2025 Moonraker обычно доступен на порту `7126`, но используйте фактический порт своей установки. Helper-функции от старого K1 могут быть несовместимы с K1C 2025.
### Как сервер увидит принтер дома

Если сервер находится в домашней сети, достаточно маршрута до принтера. Если это VPS, заранее настройте VPN/маршрутизацию, например существующий WireGuard, AmneziaWG либо Tailscale с маршрутом в домашнюю сеть. Сам K1C Control VPN не устанавливает.

Проверьте доступ **именно с сервера**, где будет бот:
```bash
curl --fail --max-time 10 http://printer.local:7126/printer/info
```

Ответ должен быть JSON Moonraker. HTML-страница Fluidd вместо JSON означает неверный порт или URL. Имя `printer.local` может работать дома и не разрешаться на VPS: замените его адресом принтера, достижимым через VPN, либо настройте DNS. Публиковать Moonraker без защиты в интернете для бота не требуется.
## 2. Получить исходники и зависимости

Команды выполняются в терминале сервера под обычным пользователем с `sudo`:
```bash
sudo apt update
sudo apt install -y git python3 python3-venv curl ca-certificates
git clone https://github.com/Xesonar/k1c-control.git
cd k1c-control
python3 --version
```

Основному боту не нужен `pip install`: он использует стандартную библиотеку Python. Дополнительные сервисы устанавливаются отдельно.
## 3. Создать Telegram-бота и узнать свой ID

1. Откройте официальный [@BotFather](https://t.me/BotFather).
2. Отправьте `/newbot`, задайте имя и username нового бота.
3. Сохраните выданный токен как пароль. Не вставляйте его в браузерную адресную строку и не отправляйте чужим ботам.
4. Откройте своего нового бота и отправьте `/start`. Пока программа ещё не запущена, ответа не будет.

Свой числовой ID можно получить напрямую у Telegram Bot API, без стороннего бота. Выполните следующий код **до запуска основного бота**. Токен вводится скрыто, не передаётся в аргументах процесса и не сохраняется этим кодом:
```bash
python3 - <<'PY'
import getpass, json, urllib.request
token = getpass.getpass('Токен нового бота: ')
try:
    url = 'https://api.telegram.org/bot' + token + '/getUpdates?timeout=0'
    with urllib.request.urlopen(url, timeout=15) as response:
        result = json.load(response)
    ids = set()
    for update in result.get('result', []):
        message = update.get('message', {})
        sender, chat = message.get('from', {}), message.get('chat', {})
        if chat.get('type') == 'private' and sender.get('id') == chat.get('id'):
            ids.add(sender['id'])
    if len(ids) == 1:
        print('Ваш owner_id:', ids.pop())
    else:
        print('Нужен один личный /start от владельца. Проверьте новый бот и повторите.')
except Exception:
    raise SystemExit('Не удалось получить обновления. Проверьте токен, интернет и отсутствие другой копии бота.') from None
PY
```

Не запускайте одновременно эту проверку и другую программу, читающую `getUpdates` того же бота. Если бот уже использовался с webhook или другим приложением, сначала разберитесь с той установкой; эта инструкция предполагает нового бота. Официальные источники: [создание бота](https://core.telegram.org/bots/tutorial#obtain-your-bot-token), [getUpdates](https://core.telegram.org/bots/api#getupdates).
## 4. Настроить и проверить конфигурацию

```bash
cp config.example.json config.local.json
chmod 600 config.local.json
nano config.local.json
```

Измените значения:

| Ключ | Значение |
| --- | --- |
| `owner_id` | Ваш числовой Telegram ID без кавычек. Ноль в примере — заглушка, с ним запуск запрещён. |
| `bot_token` | Токен нового бота, строка в кавычках. |
| `printer_url` | Базовый URL Moonraker, например `http://printer.local:7126`, без `/printer/info`. |
| `snapshot_url` | Рабочий URL JPEG-снимка, например `http://printer.local:1984/api/frame.jpeg?src=chassis`. Это URL снимка, не видеопотока. |
| `light_ws_url` | WebSocket штатного управления подсветкой, если он доступен на вашей модели; пример — `ws://printer.local:9999/`. |
| `allowed_user_ids` | Для начала оставьте `[]`: владелец имеет доступ отдельно. Остальных можно одобрить в `/admin`. |
| `web_enabled`, `media_enabled` | Для первого запуска оставьте `false`. |
| `detector_auto_enabled` | Оставьте `false`, пока детектор не установлен и не проверен. |

Без детектора можно задать `"detector_url": ""`. Если слайсер ещё не установлен, оставьте `"slicer_url": "http://127.0.0.1:3334"` и пустой `"slicer_api_key": ""`; подготовка STL тогда недоступна. Пустой `slicer_url` не допускается. В `snapshot_url` всё равно нужен корректный HTTP(S) URL: локальная проверка проверяет формат, а не доступность камеры. В URL не вставляют логины и пароли.

Проверьте JSON и обязательные поля без сети и создания состояния:
```bash
python3 bot.py --config config.local.json --check-config
```

Ожидаемый результат: `{"ok": true, "configuration": "valid"}`. Эта проверка не подтверждает доступность Telegram, камеры или принтера. Снимок при необходимости можно проверить без показа изображения:
```bash
curl --fail --max-time 15 --output /dev/null 'http://printer.local:1984/api/frame.jpeg?src=chassis'
```
## 5. Первый запуск в терминале

```bash
python3 bot.py --config config.local.json --state-dir ./state
```

Напишите `/start`, затем `/status` своему боту. Владелец должен видеть административное меню. `/files`, `/queue` и `/history` читают данные; загрузка модели не запускает её автоматически.

Другой пользователь должен сначала открыть бота и запросить доступ. Владелец одобряет запрос через `/admin`. `allowed_user_ids` используется для начального допуска; последующие решения сохраняются в SQLite. Перезапуск не должен возвращать отозванные права.

Для продолжения остановите процесс клавишами `Ctrl+C`. Не держите одновременно терминальную и systemd-копию одного бота.
## 6. Установить постоянную службу systemd

Далее используются пути, соответствующие unit-файлам репозитория:

| Путь | Содержимое |
| --- | --- |
| `/opt/k1c-telegram` | Код, читаемый службой. |
| `/etc/k1c-telegram/config.json` | Секретная конфигурация, `root:root`, режим `0600`. |
| `/var/lib/k1c-telegram` | База, очередь, аккаунты и загрузки. |
| `/var/lib/k1c-media` | Отдельное состояние и приватные видеозаписи. |

Создайте системного пользователя без входа. Если он уже есть, повторять `useradd` не нужно:
```bash
sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin k1c-runtime
sudo install -d -m 755 /opt/k1c-telegram
git archive HEAD | sudo tar -x -C /opt/k1c-telegram
sudo install -d -m 700 /etc/k1c-telegram
sudo install -m 600 config.local.json /etc/k1c-telegram/config.json
sudo install -d -m 700 -o k1c-runtime -g k1c-runtime /var/lib/k1c-telegram /var/lib/k1c-media
```

Если хотите сохранить состояние первого запуска, скопируйте его **только после остановки терминального процесса**:
```bash
sudo cp -a ./state/. /var/lib/k1c-telegram/
sudo chown -R k1c-runtime:k1c-runtime /var/lib/k1c-telegram
```

Установите основной unit и готовый drop-in для статического пользователя. Он подходит и для режима только Telegram; общий пользователь понадобится, если добавите видео:
```bash
sudo install -m 644 k1c-telegram.service /etc/systemd/system/k1c-telegram.service
sudo install -d -m 755 /etc/systemd/system/k1c-telegram.service.d
sudo install -m 644 deploy/k1c-telegram-web.conf /etc/systemd/system/k1c-telegram.service.d/90-web-runtime.conf
sudo systemctl daemon-reload
sudo systemctl enable --now k1c-telegram
sudo systemctl status k1c-telegram --no-pager
```

`LoadCredential` передаёт службе копию root-конфигурации, поэтому не нужно делать секретный файл общедоступным. Основной unit использует стандартный каталог состояния `/var/lib/k1c-telegram`. Для просмотра ошибок:
```bash
sudo journalctl -u k1c-telegram --no-pager -n 80
```

На этом установка **только Telegram** готова. Если меняете конфигурацию, редактируйте `/etc/k1c-telegram/config.json`, выполняйте `sudo python3 /opt/k1c-telegram/bot.py --config /etc/k1c-telegram/config.json --check-config`, затем перезапускайте только `k1c-telegram`.

**На свежем сервере не запускайте `deploy/runtime_cutover.py`.** Это миграция уже существующей установки с резервными копиями и старым состоянием. Шаги выше создают новую установку напрямую.
## 7. Включить веб-пульт
### 7.1. Настройки приложения

В `/etc/k1c-telegram/config.json` задайте:
```json
{
  "web_enabled": true,
  "web_host": "127.0.0.1",
  "web_port": 3340,
  "web_origin": "https://dashboard.example.com",
  "web_auth_db": "/var/lib/k1c-telegram/web.sqlite3",
  "web_static_dir": "/opt/k1c-telegram/web",
  "web_trust_proxy": true
}
```

Это **дополнительные поля**, а не замена всего файла: сохраните `owner_id`, токен и настройки принтера. Для сайта по публичному IP вместо домена укажите `https://` и свой IP. Origin не содержит пути `/api` или `/#media`.
```bash
sudo python3 /opt/k1c-telegram/bot.py --config /etc/k1c-telegram/config.json --check-config
sudo systemctl restart k1c-telegram
curl --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:3340/api/me
```

Без входа ожидается **401**. Это нормальный ответ, подтверждающий, что закрытый API работает. Порт 3340 наружу не открывают. Cookie имеет `Secure`, поэтому полноценный вход выполняют через HTTPS.
### 7.2. Первый веб-аккаунт владельца

Остановите бот на время первоначальной настройки базы:
```bash
sudo systemctl stop k1c-telegram
K1C_OWNER_ID=$(sudo python3 -c 'import json; print(json.load(open("/etc/k1c-telegram/config.json"))["owner_id"])')
```

Следующая команда запросит логин и скрытый пароль, передаст JSON через stdin и не поместит пароль в аргументы процесса. Логин: 3–64 символа `A–Z`, `a–z`, цифры, `_`, `-`, `.`; пароль: не менее 12 символов.
```bash
python3 -c 'import getpass,json,sys; print("Логин:",file=sys.stderr); u=input().strip(); p=getpass.getpass("Пароль: "); print(json.dumps({"username":u,"password":p}))' | sudo python3 /opt/k1c-telegram/web_auth.py --db /var/lib/k1c-telegram/web.sqlite3 --state-db /var/lib/k1c-telegram/bot.sqlite3 --owner-id "$K1C_OWNER_ID" bootstrap-owner
sudo chown k1c-runtime:k1c-runtime /var/lib/k1c-telegram/web.sqlite3
sudo systemctl start k1c-telegram
```

Повторный `bootstrap-owner` сбрасывает пароль владельца. Не используйте пароль Telegram. Остальные веб-аккаунты создаются владельцем в разделе доступа сайта после общего допуска пользователя. Отзыв общего допуска действует и в Telegram, и на сайте; сброс пароля закрывает старые веб-сессии.
### 7.3. HTTPS с доменом

В DNS направьте домен на сервер. На внешнем firewall разрешите 80/443. Если UFW уже используется, добавьте правила, сохранив доступ SSH; не включайте новый firewall вслепую во время удалённой сессии.
```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo install -m 644 /opt/k1c-telegram/deploy/nginx-http-settings.conf /etc/nginx/conf.d/k1c-http-settings.conf
sudo nano /etc/nginx/sites-available/k1c-control
```

Содержимое нового сайта; замените домен своим:
```nginx
server {
    listen 80;
    server_name dashboard.example.com;
    server_tokens off;
    client_max_body_size 130m;
    access_log /var/log/nginx/k1c-access.log k1c_private;

    location = /api/login {
        limit_req zone=k1c_login burst=5 nodelay;
        limit_req_status 429;
        include /etc/nginx/snippets/k1c-proxy.conf;
    }
    location / {
        include /etc/nginx/snippets/k1c-proxy.conf;
    }
}
```

Подготовьте proxy snippet, заменив заглушку доменом, без `https://`:
```bash
K1C_DOMAIN=dashboard.example.com
sed "s/__PUBLIC_IP__/$K1C_DOMAIN/g" /opt/k1c-telegram/deploy/nginx-proxy.conf.in | sudo tee /etc/nginx/snippets/k1c-proxy.conf >/dev/null
sudo ln -s /etc/nginx/sites-available/k1c-control /etc/nginx/sites-enabled/k1c-control
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
sudo certbot --nginx --redirect -d "$K1C_DOMAIN"
sudo certbot renew --dry-run
```

Certbot запросит необходимые данные и согласие с условиями. После выпуска откройте `https://` своего домена, войдите и проверьте статус. Не добавляйте nginx `alias` на каталог видео: приватные файлы выдаёт только приложение после проверки допуска. На сервере с существующими сайтами согласуйте конфигурацию с ними; приведённые шаги не требуют удалять другие сайты.
### 7.4. HTTPS по публичному IP без домена

Для этого в `deploy/` есть отдельные скрипты. Они рассчитаны на свободные порты 80/443 и отдельную инфраструктуру; не запускайте их поверх существующего веб-сервера. IP должен реально принадлежать этому серверу и принимать внешний HTTP-01 challenge. В `web_origin` должен быть тот же HTTPS IP.

Укажите свои значения; `K1C_PUBLIC_IP` — не адрес принтера:
```bash
read -r -p 'Публичный IPv4 сервера: ' K1C_PUBLIC_IP
K1C_RELEASE_DIR=/root/k1c-web-release-setup
sudo install -d -m 700 "$K1C_RELEASE_DIR"
sudo python3 /opt/k1c-telegram/deploy/prepare_infrastructure.py --release-dir "$K1C_RELEASE_DIR"
sudo python3 /opt/k1c-telegram/deploy/activate_tls.py challenge --ip "$K1C_PUBLIC_IP" --release-dir "$K1C_RELEASE_DIR"
```

После проверки доступности challenge, и только если вы согласны с условиями ACME, выполните фазы выпуска и активации:
```bash
sudo python3 /opt/k1c-telegram/deploy/activate_tls.py issue-staging --ip "$K1C_PUBLIC_IP" --agree-tos --release-dir "$K1C_RELEASE_DIR"
sudo python3 /opt/k1c-telegram/deploy/activate_tls.py issue --ip "$K1C_PUBLIC_IP" --agree-tos --release-dir "$K1C_RELEASE_DIR"
sudo python3 /opt/k1c-telegram/deploy/activate_tls.py https --ip "$K1C_PUBLIC_IP" --release-dir "$K1C_RELEASE_DIR"
sudo systemctl list-timers k1c-certbot-renew.timer
```

Подготовка устанавливает отдельный Certbot и оставляет nginx неактивным; `challenge` впервые включает HTTP, `https` — HTTPS и таймер продления. Скрипт поддерживает публичный IPv4; доменный вариант описан отдельно выше. IP-сертификат Let's Encrypt действует 160 часов, поэтому автоматическое продление обязательно: [официальное описание](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability/). Подробнее о фазах — [deploy/README.txt](../deploy/README.txt).
## 8. Добавить клипы и таймлапсы
### 8.1. Установить worker с выключенной записью

```bash
sudo apt install -y ffmpeg
ffmpeg -version
ffprobe -version
sudo install -d -m 700 -o k1c-runtime -g k1c-runtime /etc/k1c-media /var/lib/k1c-media
sudo install -m 600 -o k1c-runtime -g k1c-runtime /opt/k1c-telegram/deploy/media-config.example.json /etc/k1c-media/config.json
sudo nano /etc/k1c-media/config.json
```

В отдельной конфигурации worker задайте:
```json
{
  "media_capture_enabled": false,
  "media_state_dir": "/var/lib/k1c-media",
  "printer_url": "http://printer.local:7126",
  "media_source_url": "http://printer.local:1984/api/stream.mp4?src=chassis",
  "ffmpeg_path": "/usr/bin/ffmpeg",
  "ffprobe_path": "/usr/bin/ffprobe"
}
```

`media_source_url` — существующий HTTP/HTTPS либо RTSP-видеопоток, не JPEG `snapshot_url`. Старое имя `media_rtsp_url` тоже принимается, но в новой конфигурации используйте `media_source_url`. `printer.local` снова замените своим достижимым адресом. Учётные данные внутри URL не поддерживаются.
```bash
sudo -u k1c-runtime python3 /opt/k1c-telegram/media_worker.py --config /etc/k1c-media/config.json --check-config
sudo install -m 644 /opt/k1c-telegram/deploy/k1c-media.service /etc/systemd/system/k1c-media.service
sudo systemctl daemon-reload
```

`--check-config` проверяет пути программ и формат источника без записи и запуска FFmpeg. В **основную** конфигурацию бота добавьте `"media_enabled": true` и `"media_state_dir": "/var/lib/k1c-media"`; сохраните свой обязательный `owner_id`. Именно основной процесс проверяет общий допуск и право владельца удалять записи. Worker не требует токена Telegram и не должен получать полную конфигурацию бота.
```bash
sudo python3 /opt/k1c-telegram/bot.py --config /etc/k1c-telegram/config.json --check-config
sudo systemctl restart k1c-telegram
```
### 8.2. Явно включить запись

Когда источник и место хранения подготовлены, поменяйте в `/etc/k1c-media/config.json` `media_capture_enabled` на `true`, затем:
```bash
sudo systemctl enable --now k1c-media
sudo systemctl status k1c-media --no-pager
sudo journalctl -u k1c-media --no-pager -n 50
```

Worker записывает активные задания по свежему ID из истории Moonraker. Если сейчас печати нет, отсутствие клипа нормально; не нужно запускать печать ради проверки установки. Во время обычного задания дождитесь накопления буфера, затем запросите `/video 30` или клип на сайте. Можно выбрать 1–300 секунд. После завершения появится ускоренный таймлапс; отменённое/прерванное задание получает соответствующую отметку.

Хранение — 7 дней, архив — до 10 GiB. При заполнении новые записи приостанавливаются; удаляет их владелец. Основной процесс отменяет ожидающие запросы от пользователей с отозванным допуском. Оба процесса должны смотреть в один `media_state_dir`. Свободное место проверяют отдельно:
```bash
df -h /var/lib/k1c-media
sudo du -sh /var/lib/k1c-media
```
## 9. Подготовка STL через OrcaSlicer — дополнительно

Готовый совместимый G-code можно использовать без слайсера на сервере. STL требует отдельного worker и официального Linux OrcaSlicer; исполняемые файлы в репозиторий не включены.

1. Проверьте архитектуру сервера: `uname -m`.
2. Скачайте подходящий официальный Linux-релиз [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer/releases/tag/v2.5.0). Не используйте AppImage другой архитектуры. Зависимости установленного бинарника должны работать на вашем сервере.
3. Распакуйте AppImage и разместите его содержимое так, чтобы `/opt/orcaslicer/AppRun --help` успешно работал. Для AppImage это обычно `chmod +x ИМЯ.AppImage`, затем `./ИМЯ.AppImage --appimage-extract`; каталог `squashfs-root` содержит `AppRun` и его библиотеки. Копируйте весь каталог, а не один исполняемый файл.
4. Подготовьте доверенные профили и каталог данных. Не подключайте пользовательский JSON из Telegram-запросов.

Профили репозитория и их происхождение находятся в `profiles/`. Для получения разрешённых родительских профилей из закреплённой upstream-ревизии имеется явная команда настройки:
```bash
python3 flatten_profiles.py --output-dir ./flat-profiles
```

Она загружает профили из официального репозитория, но не устанавливает Orca и ничего не отправляет принтеру. Для service выберите проверенную цепочку machine/process/filament; полный контракт и пример конфигурации — [SLICER_README.txt](../SLICER_README.txt). В частности:
```json
{
  "api_key": "REPLACE_WITH_RANDOM_SECRET_AT_LEAST_16_CHARACTERS",
  "orca_command": ["/opt/orcaslicer/AppRun"],
  "settings_files": ["/opt/k1c-slicer-profiles/machine.json", "/opt/k1c-slicer-profiles/process.json"],
  "filament_file": "/opt/k1c-slicer-profiles/filament.json",
  "data_dir": "/var/lib/k1c-slicer/orca-data",
  "state_dir": "/var/lib/k1c-slicer",
  "bed_size": [220, 220, 250],
  "strict_slicing": true,
  "timeout_seconds": 240,
  "port": 3334
}
```

Сохраните это как `/etc/k1c-slicer/config.json` (`root:root`, `0600`; каталог `0700`), замените ключ случайной строкой и укажите реальные проверенные файлы. Профили и бинарник должны быть читаемы службой. Такой же ключ задайте в основном конфиге как `slicer_api_key`, URL — `http://127.0.0.1:3334`. Не передавайте ключ в аргументах процесса или публичных командах `curl`.
```bash
sudo install -m 644 /opt/k1c-telegram/k1c-slicer.service /etc/systemd/system/k1c-slicer.service
sudo systemctl daemon-reload
sudo systemctl enable --now k1c-slicer
sudo systemctl status k1c-slicer --no-pager
```

Unit использует отдельный DynamicUser и `LoadCredential`; не применяйте к нему drop-in основного бота. GET `/health` требует заголовок `X-Slicer-Key`; ключ можно прочитать локальным проверочным скриптом из root-конфига, не выводя его. Успешный `/health` подтверждает готовность конфигурации, но не заменяет фактическую пробную нарезку выбранным Orca. Сначала подготовьте небольшой STL без запуска печати и проверьте получившийся G-code.

STL считается в миллиметрах, масштаб не меняется автоматически. Экспортированный профиль имеет нейтральный Z offset: настройте принтер отдельно. Ограничения worker — одна нарезка, 20 MiB вход, 128 MiB результат, до 240 секунд, лимит RAM в unit. При совместной работе с детектором суммируйте лимиты памяти, оставляя место системе.

## 10. Локальный детектор — дополнительно

Без детектора бот и сайт продолжают работать. Не включайте автоматическую паузу только ради проверки наличия процесса.

1. Установите Docker по официальной инструкции для Ubuntu.
2. Если сервер маршрутизирует VPN-трафик, согласуйте настройки Docker firewall/forwarding с текущей сетью до запуска Docker. Не заменяйте целиком существующий `/etc/docker/daemon.json`.
3. Прочитайте [ai/README.txt](../ai/README.txt) и [SCORING_README.txt](../SCORING_README.txt). Модель собирается из закреплённого upstream Obico; её исходники и лицензия AGPL должны оставаться доступными.
4. Для явной установки выполните:
```bash
sudo sh /opt/k1c-telegram/ai/install.sh
curl --fail --max-time 10 http://127.0.0.1:3333/health
sudo docker logs --tail 50 k1c-obico-ml
```

Скрипт загружает исходники/веса, собирает контейнер и запускает только локальный API. Он заменяет существующий контейнер с именем `k1c-obico-ml`, если такой уже есть. Он не устанавливает apt-пакеты и не настраивает VPN.

После готовности задайте основному боту `detector_url: http://127.0.0.1:3333`, оставив `detector_auto_enabled: false`, и перезапустите бот. Кадры и реакции нужно проверить на своей установке. Публичная тестовая картинка проверяет связь с моделью, а не точность на вашем принтере. Временная оценка требует успешных наблюдений; ошибка камеры не считается чистым кадром. Автопауза включается владельцем отдельно через контроль печати.

## 11. Как пользоваться после установки

- `/status` — состояние и температуры; `/photo` — снимок; `/light` — свет.
- `/files` — подготовленные файлы; STL/G-code можно отправить боту как файл. На сайте есть загрузка и параметры STL.
- `/queue` — очередь. Следующее задание запускается вручную после освобождения стола.
- `/pause`, `/resume` — управление текущим состоянием; отмена привязывается к текущему заданию и подтверждается.
- `/video 30` — последние 30 секунд активной печати, если видео включено.
- `/admin` или раздел доступа сайта — одобрение пользователей и отзыв допуска.

Перед новым запуском освободите стол, установите PEI-пластину и подтвердите это. Система использует штатный `START_PRINT` с `NEEDCALIBRATE=1`, свежий адаптивный замер и проверку остановки до модели. Произвольный G-code без поддерживаемого старта/границ может быть отклонён. При ошибке калибровки или перезапуске автоматического продолжения нет. Ни получение файла, ни нарезка, ни добавление в очередь сами модель не запускают.

## 12. Проверки и частые проблемы

Локальные тесты проекта используют подставной принтер; выполнять реальную печать для них не нужно:
```bash
python3 -m unittest discover -p 'test_*.py' -v
python3 -m unittest discover -s deploy -p 'test_*.py' -v
```

| Симптом | Что проверить |
| --- | --- |
| `owner_id must be...` | Указан свой положительный числовой ID, не `0` и не username. |
| Бот не отвечает | Токен, исходящий HTTPS, одна копия polling, отсутствие чужого webhook. Посмотрите журнал unit. |
| Moonraker 404 / HTML | Нужен API-порт Moonraker, а не страница Fluidd. Проверьте `/printer/info` с сервера. |
| Принтер недоступен на VPS | VPN-маршрут, DNS, firewall; домашнее `.local` не обязано работать вне LAN. |
| `/api/me` возвращает 401 | До входа это ожидается. После входа проверьте HTTPS, cookie и общий допуск. |
| Ошибка CSRF / 403 | `web_origin` точно совпадает с внешним HTTPS origin, без другого домена/порта; прокси переписывает forwarding headers. Выйдите и войдите снова. |
| Видео недоступно | FFmpeg/ffprobe, источник потока, `media_capture_enabled`, общий каталог, активное задание и свежий history job ID. Буфер не возникает мгновенно. |
| Неверный интервал/нет клипа | Синхронизируйте часы сервера через NTP (`timedatectl status`), проверьте историю задания, поток и пропуски/SPS/PPS H264 в журнале. Не подменяйте пропуски старым кадром. |
| Архив заполнен | 7 дней/10 GiB; удалите ненужное через аккаунт владельца, проверьте свободный диск. |
| STL не готовится | Слайсер `/health`, совпадение ключей, Orca `--help`, полные профили и права на datadir. |
| Ошибка размера | Сайт: STL 20 MiB / G-code 128 MiB. Telegram-скачивание имеет собственный лимит; крупный G-code загрузите на принтер отдельно и выберите в файлах. |
| Печать удерживается до модели | Проверьте результат замера, температуру стола/сопла и ручную паузу. Не обходите gate через произвольный resume. |
| Не хватает RAM | Посмотрите `systemctl status`, `journalctl`, `free -h`. Orca и модель имеют отдельные значительные лимиты; сначала отключите ненужный дополнительный сервис. |

Для проверки установки достаточно локальной валидации, GET `/printer/info`, GET `/health` дополнительных сервисов и `/api/me` без входа. Не нужны команды старта, движения, прогрева, калибровки или тестовая отправка сообщений всем пользователям.

## 13. Резервная копия, обновление и откат

### Резервная копия

Планируйте короткую остановку **программных** служб. Не перезапускайте Klipper или принтер ради копирования сервера. Остановите `k1c-media`, если он установлен, затем основной бот:
```bash
sudo systemctl stop k1c-telegram
```

Когда процессы завершены, скопируйте целиком `/var/lib/k1c-telegram` (SQLite, загрузки), `/etc/k1c-telegram`, код `/opt/k1c-telegram` и установленные unit/drop-in. Для медиа добавьте `/var/lib/k1c-media` и `/etc/k1c-media`; для слайсера — его конфиг, профили и бинарник либо проверенный способ их восстановления. Например, базовая копия:
```bash
K1C_BACKUP_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -m 700 /var/backups/k1c-control
sudo tar -czf "/var/backups/k1c-control/base-$K1C_BACKUP_STAMP.tar.gz" -C / etc/k1c-telegram var/lib/k1c-telegram opt/k1c-telegram etc/systemd/system/k1c-telegram.service etc/systemd/system/k1c-telegram.service.d
sudo chmod 600 "/var/backups/k1c-control/base-$K1C_BACKUP_STAMP.tar.gz"
sudo systemctl start k1c-telegram
```

Запустите media worker обратно, если он работал. Архив содержит секреты и личные данные: храните его приватно, а внешнюю копию — в зашифрованном виде. Для копии работающей SQLite используйте её Backup API, а не один `cp bot.sqlite3` без учёта WAL.

### Обновление

1. Сохраните резервную копию и идентификатор установленного commit.
2. В отдельном checkout выполните `git pull --ff-only`, прочитайте изменения и прогоните локальные тесты новой версии.
3. Подготовьте новый каталог из `git archive HEAD`, без личного `config.local.json` и без `state/`.
4. Проверьте новый `bot.py --check-config` с существующей секретной конфигурацией.
5. Остановите установленные application-службы, замените `/opt/k1c-telegram`, оставив старый каталог для отката. `/etc/...` и `/var/lib/...` не заменяйте примерами из Git.
6. Если изменились unit-файлы, согласуйте и установите их, затем `systemctl daemon-reload`. Запустите бот и дополнительные службы, проверьте журналы и GET readiness. Не трогайте службы принтера и VPN.

### Откат

Остановите application-службы, верните предыдущий каталог кода и совместимую конфигурацию/unit, затем запустите их снова. **Сохраняйте текущие базы**, чтобы не вернуть старые права, очередь или уже выполненные команды. Восстановление старого SQLite-снимка — отдельная операция восстановления после потери данных, а не обычный откат кода. Перед понижением версии проверьте совместимость схемы состояния.

Для обновления старой DynamicUser-установки предназначен `deploy/runtime_cutover.py`: у него есть отдельные `prepare/install/rollback`, обязательный подготовленный backup, `--web-origin` для install и проверки защищённого состояния. Прочитайте [deploy/README.txt](../deploy/README.txt); это не команда первой установки.

## 14. Что не добавлять в Git

Токен бота, `config.local.json`, конфиги из `/etc`, API-ключ слайсера, пароли/сессии сайта, SQLite/WAL/SHM, загруженные модели, кадры, видео, резервные копии и закрытые VPN-ключи остаются вне публичного репозитория. Для описания проблемы достаточно версии, безопасного кода ошибки и журнала без секретов.

Исходники проекта — AGPL-3.0-only; происхождение Obico и HLS сохранено в [NOTICE](../NOTICE). OrcaSlicer, FFmpeg, Docker-образы и веса устанавливаются отдельно с соблюдением их условий.
