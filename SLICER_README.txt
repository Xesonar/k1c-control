K1C PETG slicing worker — 13 ноября 2026

Назначение
Один STL -> один G-code для фиксированного проверенного K1C/PETG-профиля.
Сервис не подключается к принтеру и не запускает печать. Он слушает только
127.0.0.1:3334; команды принтеру выполняет основной бот после подтверждения.
Зависимости сервиса: Python 3 stdlib и заранее установленный официальный OrcaSlicer.
Версия и полный профиль фиксируются оператором; самообновления нет.

Пример СЕКРЕТНОЙ конфигурации /etc/k1c-slicer/config.json (root:0600):
{
  "api_key": "REPLACE_WITH_RANDOM_SECRET_AT_LEAST_16_CHARACTERS",
  "orca_command": ["/opt/orcaslicer/AppRun"],
  "settings_files": ["/opt/k1c-slicer-profiles/fullprint.json"],
  "filament_file": "/opt/k1c-slicer-profiles/fullfilament.json",
  "data_dir": "/var/lib/k1c-slicer/orca-data",
  "state_dir": "/var/lib/k1c-slicer",
  "bed_size": [220, 220, 250],
  "strict_slicing": true,
  "timeout_seconds": 240,
  "port": 3334
}

settings_files — список доверенных файлов machine/process JSON, либо один
заранее проверенный полный merged JSON, если установленный CLI его принимает.
Подмешивать пользовательские настройки из запроса нельзя. filament_file —
заранее разрешённый PETG JSON. data_dir должен существовать, быть доступен
пользователю сервиса и содержать необходимые родительские профили, если
используется inherits. Пользовательский diff JSON не заменяет полный профиль.
Выбранная цепочка должна быть проверена фактической нарезкой контрольного STL.

Запуск (после подготовки бинарника, библиотек, профилей и orca-data):
  python3 slicer_service.py --config /path/to/config.json
Служба k1c-slicer.service использует DynamicUser + StateDirectory + LoadCredential.
Нельзя размещать секретный api_key в аргументах процесса или в исходниках.
Бот использует тот же ключ как slicer_api_key и URL http://127.0.0.1:3334.

HTTP-контракт
Оба endpoint требуют заголовок X-Slicer-Key.
GET /health -> 200 JSON:
  {"ready":true,"busy":false,"engine":"OrcaSlicer","material":"PETG"}
Если бинарник/файлы профилей/datadir недоступны, readiness false и HTTP503.
Readiness проверяет доступность конфигурации; полноценную тестовую нарезку
установленного бинарника оператор выполняет отдельно перед включением бота.

POST /slice -> body raw STL, обязательный Content-Length, максимум20MiB.
Необязательные X-Nozzle-Temperature (170–300) и X-Bed-Temperature (0–100)
задают температуры конкретного задания. Должны передаваться вместе.
На каждую нарезку создаётся отдельная копия доверенного PETG-профиля:
основной профиль и настройки соседних заданий не меняются. Стандарт 240/70.
Имя файла, JSON, 3MF, ZIP, URL и команды в запросе не принимаются.
HTTP200: raw G-code, Content-Length, application/octet-stream, максимум128MiB.
Необязательные заголовки X-Print-Time-Seconds и X-Filament-Grams получены
только из фактических комментариев Orca G-code; отсутствующие не выдумываются.
G-code передаётся по64KiB, без загрузки всего результата в оперативную память.
Ошибки JSON {"error":"code"}: unauthorized401, busy503, unavailable503,
input_too_large413, content_length_required411, invalid_stl/oversize/invalid_model422,
slice_timeout504, invalid_engine_result/output_too_large502.
При busy бот должен оставить задание в очереди, а не запускать второй процесс.

Что запускается
orca_command + --datadir DATA --load-settings MACHINE;PROCESS
  --load-filaments PETG --arrange1 --orient1 --ensure-on-bed
  --slice0 --outputdir TEMP/output --strict -- TEMP/input.stl
В argv реальные значения передаются отдельными элементами, shell не используется.
--strict включается strict_slicing=true; оператор должен проверить --help
именно установленного Linux-бинарника. --no-check/--scale не используются.
--export-3mf не нужен: --outputdir сохраняет plate_1.gcode напрямую и
не включает этап OpenGL-миниатюр. Бинарнику всё равно нужны его runtime libraries.
Сервис принимает только один plate_1.gcode. Несколько пластин, неверный код
результата, отсутствие/избыточный размер G-code считаются ошибкой.

Пределы
Проверяется binary/ASCII STL, структура треугольников, finite координаты,
до1миллиона треугольников и заведомо невозможный размер. При20MiB binary STL
практический предел числа треугольников ниже. Размещение и точная проверка
выхода за рабочую область выполняются OrcaSlicer с проверками по умолчанию.
STL не содержит единиц: считаем миллиметры; автоматического масштаба нет.
Автоориентация — эвристика, а не гарантия прочности или правильной лицевой стороны.

Отдельный процесс и лимиты systemd: RAM2300M, CPUQuota100% (один CPU),
без swap, Nice10, TasksMax128, размер одного файла256MiB. На VPS4GB
рядом с Obico допустима только одна нарезка; сложные модели могут превысить
память или время. Эти лимиты защищают остальные службы, не обещают нарезку
любой геометрии. Сетевой доступ сервиса ограничен localhost; ключи бота,
VPN и доступ к принтеру в конфигурацию слайсера не передаются.
Дедлайн240секунд контролируется внешним subprocess wait. При превышении
SIGTERM, затем SIGKILL отправляются всей отдельной группе процесса, включая
дочерние процессы. Это настоящий deadline, в отличие от проверки длительности
Orca --mstpp после нарезки. Слот освобождается после завершения/ошибки.
Временные данные очищаются после ответа. После SIGKILL всей службы могут
остаться slice-* каталоги в StateDirectory; их можно удалить при остановленной
службе. Конфигурация и токен в журнал не выводятся.

Проверки
python3 -m unittest test_slicer_service -v
Покрыты реальные HTTP, авторизация, binary/ASCII геометрия, NaN/Inf,
повреждённые файлы, размер, busy, stream/метаданные, реальные fake subprocess
exit statuses/result.json, oversized output и kill всей группы по timeout.
Это не заменяет проверку реального OrcaSlicer на выбранном профиле.

Первичные источники
https://github.com/OrcaSlicer/OrcaSlicer/releases/tag/v2.5.0
https://github.com/OrcaSlicer/OrcaSlicer/wiki/cli_mode
https://github.com/OrcaSlicer/OrcaSlicer/wiki/cli_misc
https://github.com/OrcaSlicer/OrcaSlicer/wiki/cli_transform
https://github.com/OrcaSlicer/OrcaSlicer/blob/40ecc66f78ed72dc368e0c1ebe40d77f00cc8a19/src/OrcaSlicer.cpp
