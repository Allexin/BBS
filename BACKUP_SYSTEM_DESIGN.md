# Система резервного копирования: дизайн-документ

Статус: нормативный черновик v0.2, готовится к автономной реализации  
Дата: 2026-08-25

## 1. Контекст

На машине работает устаревшая конфигурация Cobian Backup 11 Gravity. Она ежедневно зеркалирует данные с нескольких разделов одного физического диска на постоянно подключённый диск `B:`. Зеркало не хранит историю: удаление, шифрование или логическая порча исходных файлов могут быть перенесены в резервную копию.

Новая система должна создавать проверяемые версионные snapshots и файловые mirrors на отдельном физическом диске. Вне окна резервного копирования диск должен быть переведён в состояние offline.

Система создаётся как собственное Python-приложение с несколькими изолированными backup-адаптерами. Для версионных снимков формат репозитория, дедупликацию, шифрование и восстановление реализует зафиксированный движок v1 — нативный `restic.exe` с поддержкой VSS. Для файловых зеркал используется отдельный `mirror` adapter.

## 2. Цели

- Автоматически создавать версионные снимки выбранных блоков данных.
- Хранить резервные копии на отдельном физическом диске.
- Держать backup-диск offline вне ограниченного окна работы.
- Проверять личность диска до любых операций записи.
- Проверять структуру и содержимое backup-репозитория.
- Хранить полную локальную историю запусков и ошибок.
- Отправлять краткие отчёты и аварийные сообщения в Telegram.
- Публиковать read-only состояние системы в локальной сети через nginx.
- Обеспечить воспроизводимое восстановление файлов без Cobian и без GUI.

## 3. Не-цели

- Собственный формат backup-репозитория.
- Веб-управление заданиями.
- Авторизация, пользовательские сессии и роли в status UI.
- Удалённое выполнение команд через HTTP.
- Восстановление данных через веб-интерфейс.
- Полный образ операционной системы в первой версии.
- Облачное хранилище в первой версии.
- Safe mirror, quarantine удалённых/заменённых файлов и собственное версионирование mirror.
- Абсолютная защита от злоумышленника с административными правами на машине.

## 4. Основные решения

1. Управляющий и прикладной код пишется на Python, а не PowerShell.
2. Executor выбирает adapter по `job.kind`; `restic.exe` используется только adapter-ом `snapshot`.
3. Система разделена на executor, manager/scheduler и статический read-only status UI.
4. Web backend отсутствует: manager атомарно публикует обезличенные JSON-файлы, nginx раздаёт их и статический UI.
5. Ручное управление выполняется локальными CLI-командами, а не через Web.
6. Полное состояние и история хранятся в SQLite; публичная проекция состояния хранится в атомарно обновляемых JSON-файлах.
7. nginx публикует статический status UI и JSON-файлы в локальную сеть.
8. Backup-диск идентифицируется не по букве, а по набору устойчивых признаков.

## 5. Архитектура

```text
                       локальная машина

  Windows Service / Manager
       │
       ├── scheduler
       ├── job queue
       ├── SQLite state
       ├── Telegram reporter
       │
       └── запускает отдельный процесс
                    │
                    ▼
              Backup Executor
                    │
          ┌─────────┴──────────┐
          ▼                    ▼
      disk control         restic.exe
          │                    │
          └─────────┬──────────┘
                    ▼
         backup repository/mirror

  Manager ── atomic status JSON ── nginx ── LAN clients
```

### 5.1. Граница управления

HTTP-трафик технически не может достигать manager, executor, shell или привилегированных операций. Отдельного HTTP backend нет. Manager записывает только заранее определённые публичные JSON-проекции в `Stable\data\public`; nginx раздаёт их и неизменяемые UI assets из `Stable\web` как два статических read-only alias под общим URL prefix.

В первой версии допустимы только:

- `GET /backup-status/status.json`;
- `GET /backup-status/health.json`;
- `GET /backup-status/logs/index.json` и опубликованные manager-ом статические
  log-projection files;
- статические файлы status UI и отдельного logs UI.

nginx разрешает только `GET` и `HEAD`. Более специфичные locations для
`/backup-status/status.json`, `/health.json` и `/logs/` указывают на
`Stable\data\public`; HTML/CSS/JS и прочие assets — на `Stable\web`. Ни один alias не
открывает родительский Stable root или другой подкаталог `data`. Public-каталог не
содержит исполняемого кода, входящих очередей или секретов. Manager никогда не читает
файлы из `web` или `data\public`, поэтому HTTP-клиент не может использовать их как
канал команд.

## 6. Компоненты

### 6.1. Backup Executor

Executor — короткоживущий CLI-процесс. Один запуск выполняет одну конечную операцию и завершается с документированным exit code.

Предлагаемый интерфейс:

```text
backup-executor run --run-id <uuid> --job <job-id>
backup-executor check --run-id <uuid> --job <job-id> --mode <metadata|sample|full>
backup-executor prune --run-id <uuid> --job <job-id>
backup-executor restore --run-id <uuid> --job <job-id> --request-file <path>
backup-executor restore-test --run-id <uuid> --job <job-id>
backup-executor repair-mirror --run-id <uuid> --job <job-id>
backup-executor recover --run-id <uuid> --job <job-id>
backup-executor validate --job <job-id>
backup-executor validate-smart-config
```

Обязанности:

- загрузить и провалидировать конфигурацию задания;
- обнаружить разрешённый физический backup-диск и проверить его аппаратную идентичность;
- перевести диск online;
- дождаться появления заранее настроенной постоянной точки монтирования;
- проверить volume identity и marker UUID репозитория;
- собрать SMART всех физических дисков из глобального executor allowlist;
- запустить backup-движок;
- разобрать результат и выдать структурированные события;
- закрыть дочерние процессы и файловые дескрипторы;
- вернуть диск offline в секции `finally`;
- подтвердить фактическое состояние offline;
- завершиться с документированным exit code.

Executor не управляет расписанием, Telegram или общей очередью и не пишет в SQLite. Lifecycle диска полностью принадлежит executor. Все результаты передаются manager через stdout в JSON Lines. stderr предназначен только для диагностического текста UTF-8.

`--job` является идентификатором, по которому executor самостоятельно находит операционный конфиг в фиксированном каталоге конфигурации. Manager не передаёт отдельные source, disk, mount point, repository или restic-параметры.

Executor запускает `restic.exe` без shell, передавая аргументы списком. В режиме `password` passphrase читается из защищённого job-конфига и передаётся через временный password-файл с ACL только для `LocalSystem`; файл создаётся непосредственно перед запуском restic и удаляется в `finally`. Passphrase не передаётся аргументом командной строки, не сохраняется в SQLite/log/events и не попадает в environment. В режиме `none` password-файл не создаётся.

### 6.2. Manager / Scheduler

Manager — долгоживущий Windows Service и единственный владелец очереди работ.

Обязанности:

- рассчитывать расписание;
- создавать записи запусков;
- сериализовать все операции глобально;
- запускать executor как отдельный процесс;
- принимать JSON Lines из stdout executor;
- сохранять состояние и события в SQLite;
- отслеживать длительность процессов и признаки возможного зависания;
- корректно классифицировать прерванные запуски после рестарта;
- планировать job operations `backup`, `check`, `prune`, а также принимать ручные
  `restore`, `restore-test`, `repair-mirror` и `recover`, не зная их внутреннего
  алгоритма;
- отправлять Telegram-отчёты;
- атомарно публиковать обезличенные JSON-снимки для nginx.

Первоначально все операции выполняются последовательно. Каждая операция manager соответствует одному запуску executor, а executor самостоятельно переводит связанный backup-диск online и гарантированно возвращает его offline. Параллельное выполнение не входит в v1.

Manager не завершает executor из-за большой длительности и не имеет автоматического
watchdog-kill. Предполагаемое зависание является только диагностическим состоянием:
оно отражается в UI и Telegram, а решение об остановке или другом вмешательстве
принимает человек.

Manager не импортирует модули executor и не разбирает операционный конфиг. Его конфигурация содержит только:

- `job_id`;
- `enabled`;
- расписание операций;
- deadline только для индикации и alert;
- правила Telegram-уведомлений.

Источники, исключения, диск, mount point, repository, VSS, restic, retention и алгоритм проверки известны только executor. При загрузке конфигурации manager вызывает `backup-executor validate --job <id>` и принимает структурированный результат, но не повторяет валидацию самостоятельно.

Manager регистрируется существующим NSSM как Windows Service под встроенной учётной
записью `LocalSystem`, чтобы порождённый им executor наследовал права на операции с
физическим диском и VSS. Локальная пользовательская учётная запись администратора
используется для установки, управления service и `backupctl`, но service под ней не
работает и не зависит от её пароля. Отдельный privileged helper и отдельный service
user в v1 отсутствуют. Сам manager disk/VSS операции не реализует и не вызывает. Это
осознанное решение v1: web backend отсутствует, а локальные команды принимаются
только через ACL-защищённый spool.

NSSM настраивается перезапускать manager через 10 секунд после неожиданного выхода
процесса. Штатная остановка Windows Service не вызывает restart. После аварийного
старта manager сначала выполняет startup reconciliation: помечает незавершённый run
`interrupted`, проверяет process/disk state и применяет safety latch. До завершения
этой проверки и, если требуется, ручного `backupctl recover`, новые executor
operations не запускаются.

Каждый executor запускается manager-ом внутри Windows Job Object с флагом
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; запущенный executor-ом `restic.exe` и иные
дочерние процессы наследуют membership. При штатном service stop manager не закрывает
Job Object преждевременно: сначала посылает cooperative cancel и ждёт обычный
executor `finally` с disk offline. При аварийной гибели manager закрытие последнего
handle Job Object заставляет Windows завершить executor и его descendants, исключая
orphan-процессы. Такой run после restart всегда считается `interrupted`; crash-safe
recovery mirror/restic и проверка disk state обязательны до новых операций.

NSSM default shutdown settings использовать запрещено: по умолчанию после коротких
grace periods он доходит до `TerminateProcess`. Deployment оставляет включённым
только console `Control-C` для основного manager process, отключает `WM_CLOSE`,
`WM_QUIT` и `TerminateProcess` (`AppStopMethodSkip` bits `2|4|8`) и не рассылает stop
methods по дочернему process tree (`AppKillProcessTree=0`). Manager перехватывает
`Control-C`, прекращает приём новых operations, выполняет cooperative cancel и
выходит только после executor cleanup. Console wait настраивается как infinite
Windows wait (`DWORD 0xFFFFFFFF`). Конкретная установленная версия NSSM обязана
пройти integration test: длительный fake executor должен завершить cleanup позднее
обычных NSSM defaults, не получить прямой stop signal и не быть hard-killed. Если
версия NSSM не принимает infinite wait с такой семантикой, deployment блокируется до
выбора проверенного service wrapper; конечный скрытый hard-kill timeout не
подставляется.

Все source, restore target и repository/mirror paths v1 должны разрешаться в
локальные Windows volumes, доступные service context. UNC paths и пользовательские
mapped network drives запрещены и отклоняются executor validation. Локальный каталог
может одновременно публиковаться наружу через Windows SMB server: это не меняет его
классификацию как локального source. Executor всегда обращается к локальному пути, а
VSS фиксирует состояние тома независимо от открытых SMB-клиентами файлов.

### 6.3. Операции с диском

Операции с диском реализуются в отдельном Python-модуле `disk_control` и выполняются процессом executor с необходимыми локальными привилегиями.

Публичный программный интерфейс модуля ограничен:

```text
inspect(executor_job_config) -> DiskObservation
bring_online(verified_disk) -> None
ensure_repository_path(executor_job_config, timeout) -> VolumeObservation
take_offline(verified_disk) -> None
```

Модуль не принимает данные из web-каталога. Все параметры происходят из провалидированной локальной конфигурации executor.

Mount point является внутренней деталью executor. После перевода диска online executor вызывает идемпотентную операцию `ensure_repository_path()`: использует восстановленный Windows путь, а при его отсутствии безопасно назначает конфигурированный путь заново. Manager не знает, существует ли mount point и потребовалось ли перемонтирование.

Предпочтительная реализация — Windows API через `ctypes`/`pywin32`: перечисление физических дисков и томов и изменение offline-атрибута диска через документированный Windows storage API. PowerShell, `diskpart` и разбор локализованного консольного вывода не используются в штатном пути. Если PoC покажет, что прямой API ненадёжен, fallback должен быть оформлен отдельным ADR до реализации.

### 6.4. Status projection

Отдельного Status Publisher нет. Manager строит публичную проекцию и атомарно публикует два UTF-8 JSON-файла:

- `status.json` — текущее состояние и сводка заданий;
- `health.json` — heartbeat manager и версия схемы.

Запись выполняется как `write temp → flush → fsync → os.replace`. JSON никогда не изменяется на месте. При ошибке публикации manager продолжает backup, фиксирует warning и уведомляет Telegram, если ошибка сохраняется дольше заданного порога.

### 6.5. nginx

nginx отвечает за:

- публикацию статического UI;
- публикацию JSON-проекций;
- сетевые ограничения локальной сети;
- access/error logs.

Потоковые протоколы, backend proxy, WebSocket и Server-Sent Events не используются. UI периодически выполняет обычный `GET /backup-status/status.json`. Для редко изменяющегося backup-состояния этого достаточно.

## 7. Модель заданий

### 7.1. Независимость jobs

Job — логическая самодостаточная единица с единым `job_id`, но её конфигурация
физически разделена границей компонентов: manager хранит только display name,
schedule/cycle/deadline, executor — source, adapter kind, destination, retention и
verification. Отдельной сущности «блок данных» в модели v1 нет.

Несколько jobs могут независимо указывать один и тот же source. Например:

```text
archive-basovs-mirror   kind=mirror    source=T:\
archive-basovs-history  kind=snapshot  source=T:\
```

Такие jobs:

- не знают друг о друге;
- не разделяют success/failure state;
- могут иметь разные destinations и расписания;
- не образуют транзакцию;
- не требуют специальной координации manager;
- сериализуются общей очередью v1 как любые другие jobs.

Предварительные jobs для миграции с Cobian:

| ID | Текущий источник | Назначение |
|---|---|---|
| `archive-basovs` | `T:\` | пользовательский архив |
| `archive-bbs` | `U:\` | архив BBS |
| `data` | `F:\` | рабочие данные |
| `servers` | `S:\` | данные сервисов; требуется отдельный аудит консистентности |
| `agentdvr-settings` | `C:\Program Files\Agent\Media\XML` | настройки AgentDVR |

Диски `I:`, `M:` и `V:` пока не классифицированы и не должны автоматически включаться в backup.

### 7.2. Пример разделённой конфигурации задания

Фрагмент manager config:

```yaml
jobs:
  - id: data
    enabled: true
    display_name: Рабочие данные
    schedule:
      timezone: Europe/Samara
      cron: '5 0 * * 1'
      deadline: '08:00'
      cycle:
        - {operation: backup}
        - {operation: backup}
        - {operation: backup}
        - {operation: backup}
        - {operation: check, mode: subset}
```

Соответствующий `jobs/data.yaml`, читаемый только executor:

```yaml
schema_version: 1
id: data
kind: snapshot
display_name: Рабочие данные
source:
  path: 'F:\\'

excludes:
  - 'System Volume Information'
  - '$RECYCLE.BIN'

repository: primary

retention:
  last: 1
  daily: 0
  weekly: 4
  monthly: 6
  yearly: 0

verification:
  data_subset_parts: 4
```

Оба файла имеют формат UTF-8 YAML без BOM и запрещают неизвестные поля. Manager
проверяет только scheduler-половину; операционный job-конфиг проверяет только
executor. Связь ограничена совпадающим `job_id`.

### 7.3. Конфигурация репозитория

```yaml
id: primary

disk:
  serial: '<physical-disk-serial>'
  expected_size_bytes: 4000787030016
  partition_guid: '<partition-guid>'
  volume_label: 'BACKUP'
  marker_uuid: '<random-uuid>'
  mount_point: 'C:\\BackupMount'

engine:
  type: restic
  repository_path: 'C:\\BackupMount\\restic'
  encryption:
    mode: none
```

Для password-protected snapshot вместо `mode: none` используется:

```yaml
encryption:
  mode: password
  passphrase: '<repository-passphrase>'
```

Job-конфиг защищён ACL для Administrators и `LocalSystem` и не публикуется. Passphrase хранится переносимо прямо в этом конфиге, а не в DPAPI, чтобы копия конфига позволяла восстановление на другой машине. Telegram token остаётся отдельным manager secret и не относится к snapshot encryption.

## 8. Жизненный цикл backup-диска

Номинальная последовательность:

```text
OFFLINE
  → обнаружить диск
  → сверить доступную аппаратную идентичность
  → ONLINE
  → обеспечить доступность сконфигурированного repository path
  → сверить volume identity и marker UUID
  → открыть и проверить restic repository
  → выполнить backup
  → выполнить требуемую проверку
  → закрыть процессы и файловые дескрипторы
  → OFFLINE
  → проверить OFFLINE
```

Инварианты:

- неизвестный диск никогда не переводится online автоматически;
- несовпадение хотя бы одного обязательного идентификатора блокирует запись;
- буква диска не является идентификатором;
- backup не начинается, если диск нельзя гарантированно вернуть offline;
- после старта manager по истории событий определяет незавершённый disk lifecycle,
  устанавливает глобальный safety latch и требует ручной operation `recover`;
- перевод offline не выполняется, пока подтверждённо работает разрешённый backup-процесс;
- repository path находится вне пользовательских каталогов и не публикуется по сети;
- необходимость повторного назначения mount point является внутренней деталью executor и не меняет внешний контракт.

Программный offline уменьшает поверхность атаки, но не защищает от злоумышленника с правами администратора. Возможность физического отключения питания диска рассматривается как отдельное будущее улучшение.

## 9. Backup-движок

### 9.1. Требования

- нативный Windows CLI;
- отсутствие подписки и обязательного внешнего сервиса;
- версионные снимки;
- дедупликация;
- контрольные суммы;
- встроенный формат restic с явным режимом доступа `none|password`;
- VSS для открытых файлов;
- политики retention;
- полная и выборочная проверка данных;
- восстановление отдельных файлов и деревьев;
- машинно-читаемый вывод и документированные exit codes;
- устойчивость к прерыванию процесса.

### 9.2. Решение v1

Snapshot engine v1 — restic. Сравнительный выбор с другими движками не выполняется.

Proof of concept обязан подтвердить restic на Windows 10 с Unicode-путями, VSS, длинными путями, аварийным завершением, check и restore. Неуспешный PoC блокирует snapshot adapter и требует отдельного ADR; автоматического fallback на Kopia или другой engine нет.

## 10. Состояния и результаты

Состояния запуска:

```text
queued
waiting_for_repository
identifying_disk
preparing_repository
snapshotting
backing_up
verifying
retention
taking_offline
success
warning
failed
cancelled
interrupted
```

Терминальные результаты:

- `success` — снимок создан, обязательные проверки пройдены, диск возвращён offline;
- `warning` — обязательный data result получен, но возникло документированное
  нефатальное отклонение; штатные configured excludes сами по себе warning не дают;
- `failed` — снимок не создан либо обязательный инвариант нарушен;
- `interrupted` — предыдущий manager или executor завершился без терминального события.

Успешное создание снимка и успешный возврат диска offline — разные поля. Запуск с созданным снимком, но оставшимся online диском не должен отображаться зелёным.

## 11. Протокол событий

Executor пишет в stdout по одному JSON-объекту на строку. stderr сохраняется отдельно как диагностический поток.

```json
{"schema_version":1,"event":"run_started","run_id":"...","job_id":"data","timestamp":"..."}
{"schema_version":1,"event":"stage_changed","stage":"backing_up","timestamp":"..."}
{"schema_version":1,"event":"progress","stage":"backing_up","files_done":12000,"files_total":50000,"bytes_done":123456789,"bytes_total":987654321,"timestamp":"..."}
{"schema_version":1,"event":"snapshot_created","snapshot_id":"abc123","bytes_added":3456789}
{"schema_version":1,"event":"run_finished","result":"success","timestamp":"..."}
```

Требования:

- UTF-8 без зависимости от системной code page;
- номер версии схемы в каждом событии;
- неизвестное событие не ломает manager;
- событие не содержит секретов;
- пути в публичных status JSON не публикуются;
- прогресс может теряться без нарушения итоговой истории;
- терминальные события сохраняются транзакционно.

Каждая активная operation публикует `stage` и время начала stage. Progress не имеет
отдельного real-time timer или фонового потока: его публикация встроена в основную
логику executor и выполняется только в естественных безопасных точках — после
завершения файла, порции сканирования или иного атомарного шага. В такой точке
событие публикуется, если с предыдущего прошло ориентировочно 20 секунд; интервал не
является строгим контрактом. Если обработка одного большого файла длится дольше,
следующее обновление появляется только после завершения этого файла.

Для измеримых стадий executor по возможности публикует
`files_done/files_total`, `bytes_done/bytes_total` и среднюю скорость. ETA показывается
только при известном total и достаточно устойчивой скорости; отсутствие ETA не
является ошибкой. Для неизмеримых стадий (`disk_online`, `vss`, `retention`,
`disk_offline` и аналогичных) UI и CLI показывают название и elapsed time, но не
выдуманный процент. Manager сам знает, что дочерний executor process ещё существует;
это не требует heartbeat-событий от executor.

События `progress` являются только транзитным каналом между executor и manager.
Manager заменяет ими текущие progress-поля активного run и публичную status
projection, но не пишет их в дневной JSONL и не добавляет в `run_events`. После
terminal result отдельная история промежуточного progress не сохраняется.

## 12. Хранение состояния

SQLite хранит:

- задания и их отображаемые метаданные;
- запуски;
- стадии и итоговые результаты;
- снимки backup-движка;
- агрегированные метрики;
- предупреждения и ошибки;
- Telegram delivery status;
- состояние репозиториев;
- ближайшие рассчитанные запуски.

SQLite не является единственным источником сведений о наличии backup. Истиной для содержимого остаётся сам backup-репозиторий; база manager может быть восстановлена его инспекцией.

Большие stdout/stderr logs хранятся отдельными UTF-8 файлами с ротацией. SQLite содержит ссылки, размеры, хэши и важные структурированные события.

Ротация логов только календарная, без size limit: в `00:00` по configured local
timezone manager открывает новый дневной UTF-8 JSON Lines log и удаляет дневные log-файлы старше
60 суток. Операция, пересекающая полночь, не прерывается и продолжает писать в новый
файл. Записи содержат component, operation_id и run_id, поэтому события одного run
можно собрать из двух соседних дневных файлов.

Ротация не имеет отдельного timer/thread. При старте обычная инициализация logger
определяет файл текущей локальной даты, открывает его в append mode и выполняет
очистку файлов старше 60 суток. Далее перед добавлением каждой JSONL-строки logger
сравнивает текущую локальную дату с датой открытого файла. При смене даты он сначала
закрывает и flush-ит старый файл, затем открывает файл нового дня и повторяет ту же
очистку. Поэтому «ротация в 00:00» означает первую запись после наступления новой
локальной даты; при отсутствии записей никакой фоновой работы не выполняется.

Каждая физическая строка является одним самостоятельным JSON-объектом и как минимум
содержит schema version, стабильный `event_id`, timestamp, severity, component,
event/message и применимые operation_id/run_id/job_id. Переводы строк и произвольный stderr сохраняются только
как экранированные значения JSON, поэтому одна запись не разрушает границы строк.
CLI-интерфейс просмотра логов отсутствует. Локальный администратор при необходимости
читает исходные JSONL-файлы напрямую; Web UI использует отдельную статическую
проекцию, подготовленную manager-ом.

Поле `timestamp` всегда хранится в UTC в RFC 3339 с суффиксом `Z`. Имя дневного
файла, переключение в `00:00` и возраст 60 суток рассчитываются по configured local
timezone. Переходы timezone/DST не меняют порядок событий внутри файлов, поскольку
сами timestamps остаются UTC.

Дневной JSONL является локальным административным журналом под ограничивающим ACL и
может содержать полный source/destination path для конкретной ошибки чтения, записи,
удаления или verification. Публичные `status.json` и Web UI никогда не получают эти
пути и используют job display name, stage и безопасную краткую причину. Telegram
считается приватным каналом единственного оператора и может включать релевантный
абсолютный путь — например, проблемный файл или сохранённую `.restore-incomplete`
папку — если это помогает немедленно разобраться с alert. Passphrase, Telegram token,
содержимое файлов, serial/WWN и иные секреты/идентификаторы устройств туда всё равно
не отправляются.
Пароли restic, содержимое password file, секреты Telegram, секретные environment
variables и командные строки с ними запрещено записывать на любом уровне логирования.

Постоянный JSONL содержит начало operation/run, переходы stage, warnings/errors и
terminal result. Он не содержит progress events и не создаёт записи для каждого
успешно обработанного файла. Набор переходов stage должен позволять восстановить
длительность крупных этапов и определить этап отказа без подробного per-file trace.
После каждой полной JSONL-строки logger выполняет обычный userspace `flush`. Для
записей severity `warning`/`error` и terminal result дополнительно вызывается Windows
`FlushFileBuffers`, чтобы критичная последняя диагностика не оставалась только в
кэше ОС. Частые progress events через этот путь не проходят.

Web Logs UI никогда не раздаёт исходный JSONL. Manager формирует отдельные
санитизированные статические log projections: сохраняются timestamp, severity,
component, event, job display name, operation_id, run_id, operation kind, stage и
безопасная причина; полные filesystem paths, device identities, command lines,
environment и secrets удаляются до записи в web-root. Исходный и публичный журналы
считаются разными артефактами, а не двумя nginx-маршрутами к одному файлу.

Публичные log projections разделены по локальным календарным датам и имеют тот же
retention 60 суток. `logs/index.json` перечисляет только доступные даты и имена
статических projection files. Logs UI по умолчанию загружает только сегодняшний
файл; пользователь может выбрать другую доступную дату. Все 60 суток автоматически
не загружаются. После загрузки выбранного дня браузер локально фильтрует записи по
severity, job, operation ID и тексту. Фильтры не создают HTTP-запросов к manager и не
являются управляющим каналом.

Каждая дневная Web-проекция является цельным JSON-массивом. При новом постоянном
событии manager формирует полную новую версию в sibling temporary file, flush-ит и
закрывает её, после чего заменяет целевой файл через Windows rename/replace primitive.
Он никогда не дописывает публичный JSON in-place. Браузер поэтому видит старую либо
новую целую версию. Если во время подмены GET временно возвращает отсутствие файла,
sharing error или сетевую ошибку, Logs UI сохраняет последнюю успешно загруженную
версию на экране, показывает её возраст и повторяет запрос позднее; неуспешный GET не
заменяет данные пустым списком.

Logs UI никогда автоматически не перезагружает уже открытую дневную проекцию:
содержимое и позиции строк остаются стабильными, пока пользователь анализирует лог.
`logs/index.json` для каждой даты содержит generation/hash, record count и
`updated_at`. UI может периодически читать только этот маленький index; если
generation выбранного дня изменилась, поверх страницы появляется ненавязчивая
плашка «Лог изменился — обновить». Новая проекция загружается исключительно после
явного нажатия пользователем. Плашка не перекрывает строки журнала и не сбрасывает
текущие фильтры.

При ручном обновлении Logs UI сохраняет выбранные фильтры и `event_id` верхней
видимой записи. После загрузки новой generation он находит тот же event и
восстанавливает позицию viewport; появившиеся записи не перескакивают перед глазами.
Если сохранённая запись исчезла, UI оставляет ближайшую доступную позицию и явно
сообщает, что точный anchor больше не найден. Один и тот же `event_id` используется
в исходном JSONL и санитизированной Web-проекции.

Записи отображаются в прямом хронологическом порядке: старые сверху, новые снизу.
При первом открытии сегодняшней даты Logs UI прокручивает список к последней записи.
Как только пользователь прокрутил список или выбрал фильтр, автоматическое изменение
позиции запрещено; только явные действия пользователя меняют viewport.

Logs UI не объединяет дневные файлы. Фильтр и ссылка `operation_id` действуют только
внутри выбранной даты. Если operation пересекла локальную полночь, пользователь сам
переходит к соседнему дню; отдельный cross-day index или режим операции в v1 не
создаётся.

Сбой записи диагностического JSONL сам по себе не останавливает data operation, если
SQLite и канал executor→manager продолжают работать. Manager помечает системный
logging health как `critical`, сохраняет доступную структурированную ошибку в SQLite
и отправляет Telegram-alert только при первом log-write failure данной operation.
Повторные сбои в той же задаче независимо от run/stage лишь увеличивают
агрегированный счётчик и не создают новых сообщений; счётчик и затронутая job
включаются в ближайший суточный heartbeat. Следующая operation получает собственный
лимит в один alert, если проблема сохраняется.
Первая последующая JSONL-запись, для которой успешно завершились требуемые `flush` и,
где применимо, `FlushFileBuffers`, автоматически снимает logging health `critical`.
Отдельное Telegram recovery-сообщение не отправляется; факт и время восстановления
включаются в ближайший суточный heartbeat.

## 13. Read-only status projection

### 13.1. Сводный статус

nginx статически раздаёт `GET /backup-status/status.json`:

```json
{
  "schema_version": 1,
  "generated_at": "2026-08-25T22:00:00+04:00",
  "manager": {"state": "idle", "last_seen_at": "..."},
  "repository": {
    "id": "primary",
    "disk_state": "offline",
    "last_verified_at": "...",
    "free_bytes": 123456789
  },
  "jobs": [
    {
      "id": "data",
      "health": "healthy",
      "last_success_at": "...",
      "next_run_at": "...",
      "last_snapshot_id": "abc123"
    }
  ]
}
```

### 13.2. Обновление статуса

UI периодически выполняет:

```http
GET /backup-status/status.json HTTP/1.1
```

Предварительный интервал опроса — 10 секунд при открытой странице. Сервер всегда возвращает самодостаточный снимок, поэтому пропущенные обновления и переподключение не требуют отдельной логики восстановления потока.

nginx публикует стандартные `ETag` и `Last-Modified`, а клиент выполняет условные запросы. Manager не реализует HTTP.

### 13.3. Публичность в LAN

Поскольку status UI доступен всем в локальной сети, он не должен раскрывать:

- секреты и токены;
- полные локальные пути с чувствительными именами;
- содержимое backup-файлов;
- серийные номера дисков;
- имена локальных учётных записей;
- командные строки с секретными environment variables;
- raw stderr без фильтрации.

Детальные исходные журналы остаются на машине и доступны администратору напрямую
через файловую систему. Отдельных `backupctl logs` или иных CLI-команд для чтения
логов в v1 нет.

## 14. Локальное управление

Управление выполняется локальным CLI manager-а:

```text
backupctl status
backupctl jobs list
backupctl config validate
backupctl queue list
backupctl queue remove <operation-id>
backupctl run <job-id>
backupctl cancel-current
backupctl check <job-id> --mode <subset|full>
backupctl restore <job-id> --version <latest|snapshot-id> \
  --path <relative-source-path> --target <absolute-parent-directory>
backupctl restore-test <job-id>
backupctl repair-mirror <job-id>
backupctl recover <job-id>
backupctl disk status <job-id>
```

`backupctl` создаёт JSON-команду во временном файле и атомарно перемещает её в ACL-защищённый spool-каталог manager. Manager обрабатывает только файлы с валидной схемой и UUID, затем перемещает их в `accepted`, `completed` или `rejected`. Каталог доступен на запись только локальной группе Administrators и `LocalSystem`. HTTP и сокеты для управления не используются.

Исключение — `backupctl config validate`: это прямой локальный offline validator, а
не spool-команда manager. Он работает при остановленном service, строго проверяет
manager YAML и вызывает executor validation для каждого job YAML. Команда не создаёт
operation/run, не меняет SQLite, не выполняет disk/VSS lifecycle и не отправляет
Telegram. Она печатает результат каждого файла и возвращает non-zero exit code, если
хотя бы один config не прошёл чтение или schema/semantic validation.

Каждая принятая operation получает стабильный UUID `operation_id`, не зависящий от
её текущей позиции. Команда, создавшая operation, печатает этот ID; `backupctl queue
list` показывает active и весь queued-хвост с ID, status, job, kind, trigger и
временем ожидания. Позиция является только отображаемым вычисляемым значением и не
используется как адрес операции.

`backupctl queue remove <operation-id>` удаляет только operation со статусом
`queued`. Если ID не существует, operation уже terminal либо уже перешла в
`running`, команда ничего не меняет и возвращает явный соответствующий результат.
Running operation этой командой не останавливается; для неё существует только
`backupctl cancel-current`. Удаление записывается в локальную историю с timestamp и
причиной `manual_queue_remove`. Если удалена scheduled operation, её cycle не
продвигается: удаление является невыполненной фазой, поэтому на следующем cron-slot
job снова создаёт ту же operation kind.

Команда `queue remove` не запрашивает дополнительного интерактивного подтверждения:
точный UUID, локальный административный доступ и ограничение только статусом
`queued` считаются достаточным подтверждением. CLI печатает структурированный
result и использует разные exit codes для `removed`, `not_found`, `not_queued` и
ошибки manager-а.

Удаление не стирает строку SQLite: operation атомарно переходит из `queued` в
терминальный статус `removed` с `removed_at` и `manual_queue_remove`. Она сразу
исчезает из рабочей очереди, остаётся в локальной истории и больше не считается
unfinished при подавлении дублей; последующее cron-срабатывание может создать новую
operation той же job и kind.

При каждом старте manager все найденные в SQLite operations со статусом `queued`
атомарно переводятся в терминальный `discarded_on_restart` до расчёта новых cron
triggers. Они никогда не исполняются автоматически после restart. Для scheduled
operation cycle/slot counter не продвигается, поэтому соответствующая фаза вернётся
только в следующий штатный cron-slot; manual operation оператор при необходимости
создаёт заново. Startup Telegram report агрегированно перечисляет job, kind и trigger
всех отброшенных operations без отдельных сообщений на каждую.

При штатном Windows Service stop manager до ожидания cleanup активного executor
атомарно переводит весь ожидающий хвост из `queued` в терминальный
`discarded_on_service_stop`. При следующем старте эти записи не
переклассифицируются. `discarded_on_restart` применяется только к необработанным
queued-записям, оставшимся после аварийного завершения manager. Оба результата не
продвигают cycle и сохраняются в истории с timestamp и исходным trigger.
Во время shutdown отдельные Telegram-сообщения об отброшенном хвосте не отправляются.
Первый startup report после следующего запуска включает их компактным списком с
job, kind и явной причиной `service stop`, отдельно от аварийно отброшенных
`discarded_on_restart` operations.

Если при штатном stop активный executor принял cooperative cancel, завершил cleanup
и вернул терминальное событие, его run получает result
`cancelled_by_service_stop`, а не `interrupted`. Такой result не продвигает cycle и
попадает в следующий startup report. `interrupted` зарезервирован для run, у которого
manager после аварии не нашёл надёжного terminal event.

Manual queue removal не создаёт немедленный Telegram-alert, поскольку это явное
действие локального оператора. Ближайший суточный heartbeat перечисляет удалённые
operation с job и kind; для scheduled operation дополнительно сообщает, что cycle не
продвинут и та же фаза ожидается в следующий cron-slot.

`cancel-current` не принимает `job-id`: в v1 одновременно работает не более одного
executor. Команда посылает cooperative cancel именно текущему executor, после чего
тот обязан пройти обычный `finally`, завершить принадлежащие ему дочерние процессы и
вернуть backup-диск offline. Основное назначение команды — интеграционные и
аварийные тесты lifecycle. Она не считается средством восстановления зависшей job:
при настоящем зависании оператор сначала исследует процессы и сам решает, какой из
них останавливать средствами Windows.

Если cooperative cancel и cleanup завершились терминальным событием, run получает
result `cancelled_by_operator`. Он не продвигает scheduled cycle. Это значение
отличается от `cancelled_by_service_stop` и аварийного `interrupted`, чтобы причина
остановки оставалась однозначной в локальной истории и отчётах.

Успешный `cancelled_by_operator` не создаёт немедленный Telegram-alert, поскольку
оператор сам инициировал действие; он включается в ближайший суточный heartbeat.
Ошибка cooperative cleanup, VSS cleanup либо отсутствие подтверждённого disk offline
классифицируется и оповещается по собственным правилам, включая немедленный critical
alert для disk lifecycle failure, и не скрывается причиной cancel.

`backupctl run <job-id>` имеет семантику «выполнить следующей», а не «добавить ещё
один экземпляр». Если operation этой job уже `queued`, manager перемещает её в
начало ожидающей очереди, не создавая дубль. Если она уже `running`, команда
завершается без создания operation. Если незавершённой operation нет, manager
создаёт одну ручную operation в начале ожидающей очереди. Текущий executor команда
не прерывает.

Интерактивная команда, создавшая operation (`run`, `check`, `restore`,
`restore-test`, `repair-mirror`, `recover`), по умолчанию остаётся подключённой к её
статусу до terminal result. Пока operation ожидает, CLI показывает активную job,
её stage и elapsed time, позицию своей operation в очереди и не реже раза в минуту
печатает локальное обновление, подтверждающее, что manager и исполнительный процесс
по-прежнему работают, а ожидание продолжается. После старта CLI показывает общий
stage, stage elapsed, возраст последнего progress event и доступные счётчики
files/bytes, скорость и ETA. Смена stage выводится немедленно. Долгое отсутствие
нового progress event само по себе не считается зависанием: текущий файл или шаг
может выполняться дольше интервала публикации.

`Ctrl+C` в таком наблюдающем CLI завершает только локальное ожидание и вывод. После
того как manager принял request, это не удаляет queued operation и не посылает cancel
активному executor. Для отмены используется только отдельная явная команда
`backupctl cancel-current`; повторный `backupctl status` позволяет снова наблюдать
состояние оставшейся operation.

Исключение: если текущая cycle phase job зафиксирована на failed/inconclusive check
либо установлен `recovery_check_required`, `backupctl run` отклоняется с
`verification_required`; operation не создаётся и очередь не меняется. До успешной
проверки разрешены manual check, manual `repair-mirror` для mirror и, при отдельном
disk-lifecycle latch, manual recover.

Опасные операции требуют явного подтверждения и не входят в MVP:

- удаление снимков вне retention;
- инициализация нового репозитория;
- смена разрешённого физического диска;
- восстановление поверх источника.

## 15. Расписание и обслуживание

Предварительная эксплуатационная модель:

- основная backup/check job соответствующей ночи обычно срабатывает в начале ночного
  периода, типичный cron — `00:05`;
- каждая крупная backup job выполняется один раз в неделю в назначенную ей ночь;
- типичная неделя распределяет разные jobs по разным ночам: библиотека, фотографии, проекты и другие наборы;
- целевой RPO обычного набора — около 7 дней; точный stale threshold задаётся job config;
- на одну ночь обычно назначается одна основная крупная backup job;
- retention snapshot job выполняется внутри того же backup-run после успешного
  создания snapshot;
- prune оформляется одной repository-wide maintenance job на репозиторий и обычно
  ставится cron раз в месяц на отдельную ночь;
- prune не запускается автоматически после backup или по порогу свободного места;
- обычный check является фазой того же job cycle и в свою ночь заменяет backup,
  типично после четырёх успешных backup-фаз;
- полный recovery check запускается вручную либо автоматически требуется после
  потери scheduler/check cursor state, но не имеет отдельного регулярного cron;
- автоматического restore-test нет;
- один обязательный ручной restore-test выполняется примерно через месяц после
  ввода системы в эксплуатацию; последующие — после обновления restic/restore-кода
  либо по решению оператора.

Точные расписания определяются после измерения первого полного backup и типичного суточного объёма изменений.

SMART не имеет отдельного schedule: он собирается как preflight-стадия каждого
executor run до основной backup/check/prune работы.

## 16. Telegram

Manager отправляет один обязательный суточный heartbeat-отчёт независимо от того,
были ли запуски и ошибки. Отчёт покрывает период после предыдущего успешно
сформированного суточного отчёта и содержит:

- список выполненных backup jobs с результатом и длительностью;
- компактный список всех ошибок за период;
- если ошибок нет — явную строку `Ошибок нет`;
- если backup jobs не выполнялись — явную строку `Backup jobs не запускались`;
- текущее общее health-состояние системы.

Успешное завершение отдельной job не создаёт немедленное Telegram-сообщение. До
суточного отчёта не откладываются аварийные события, требующие внимания:

- failure любой operation, включая ошибки чтения, записи и проверки;
- ошибка идентификации/подготовки диска или неподтверждённый возврат offline;
- наложение jobs;
- фактический deadline overrun;
- критически низкое свободное место;
- отсутствие свежего успешного backup по SLA;
- startup report после простоя manager.

Срочный alert не исключает событие из следующего суточного отчёта: там оно
показывается краткой строкой в общем списке ошибок. Повторные сообщения одного и
того же alert дедуплицируются по типу события и run.

Время heartbeat-отчёта не зашито в код: оно задаётся cron-выражением и timezone в
manager config.

Для heartbeat действует та же политика без catch-up: если manager не работал в
момент `daily_report_cron`, пропущенный отчёт после запуска не досылается. Его
заменяет startup report, содержащий сведения о простое и пропущенных operations;
следующий heartbeat отправляется в очередной штатный момент.

Telegram является каналом уведомлений, но не управления.

## 17. Наблюдаемость и health

Health блока данных рассчитывается, а не задаётся вручную.

Пример критериев:

- `healthy`: последний run успешен, последний успешный backup свежее SLA,
  обязательная проверка успешна, диск offline;
- `warning`: последний backup-run завершился ошибкой, но предшествующая успешная
  копия ещё свежее SLA; либо проверка просрочена или осталось мало места;
- `critical`: нет успешной копии свежее SLA, подтверждено повреждение репозитория
  либо диск не вернулся offline;
- `unknown`: manager ещё не получил достаточных данных.

Таким образом, единичный failed run не объявляет уже существующую пригодную копию
потерянной. Он повышает job health до `warning`; состояние становится `critical`,
только когда возраст последнего успешного backup превысил настроенный freshness SLA
либо обнаружена самостоятельная критическая проблема.

Успешный backup-run не перекрывает результат проверки целостности. Если `check`
подтвердил повреждение repository, snapshot или mirror data, job health немедленно
становится `critical` и остаётся таким до успешной проверки, подтверждающей
исправное состояние данных (обычно после диагностированного и выполненного
человеком восстановления/исправления).

Любой неуспешный terminal result операции `check` также даёт `critical`, даже если
повреждение не было доказано. Невозможность открыть repository, включить или
идентифицировать диск, прочитать проверяемые данные, запустить checker либо довести
проверку до конца означает, что пригодность backup неизвестна и требуется срочное
вмешательство. Категории `check_corruption_found` и `check_inconclusive` сохраняются
раздельно для диагностики, но имеют одинаковый health severity. Только последующий
успешный `check` снимает этот critical.

Dashboard должен отдельно показывать:

- свежесть снимка;
- свежесть проверки;
- результат последнего запуска;
- состояние диска;
- свободное место;
- непрочитанные файлы;
- активную стадию и прогресс.

## 18. Восстановление

Backup считается рабочим только при наличии проверенного процесса восстановления.

MVP поддерживает:

- просмотр списка снимков через локальный CLI;
- восстановление выбранного файла или каталога в новую пустую папку внутри явно
  указанного локального target-каталога;
- ручной restore-test фиксированной контрольной выборки;
- сверку восстановленных данных;
- журнал результата восстановления.

`restore-test` не допускается в scheduler `cycle` v1 и запускается только локальной
командой. Первый post-launch test планируется примерно через один месяц эксплуатации.
Срок хранится во внешнем календаре оператора: manager не создаёт reminder, не
показывает просрочку и не меняет health из-за отсутствия restore-test. Результат
фактически выполненной ручной операции сохраняется в обычной истории runs.

Восстановление поверх оригинальных данных запрещено в MVP.

`backupctl restore` принимает один относительный путь внутри source root и один
абсолютный локальный parent target. Явное значение `--path .` означает весь source
root. Для source selection запрещены absolute path, drive letter, `..`, wildcard,
glob и пустая строка. Target parent должен существовать,
быть обычным локальным каталогом, не reparse point и быть доступным для записи.

Если выбран конкретный relative path, он сохраняется целиком ниже технической папки
результата: `--path Photos\2020` создаёт
`BackupRestore-...\Photos\2020\...`, а выбор одного файла сохраняет его вместе с
родительскими компонентами logical path. `--path .` является единственным
исключением и помещает содержимое всего source root непосредственно в
`BackupRestore-...`. Snapshot и mirror используют одинаковую семантику layout.

Manager создаёт request UUID. Executor внутри указанного parent атомарно создаёт
новую технически именованную папку вида
`BackupRestore-<job-id>-<UTC>-<request-id>` и до первой записи подтверждает, что она
не существовала и пуста. Затем executor создаёт `.restore-incomplete` marker и
обеспечивает его запись на диск до начала восстановления данных. Marker удаляется
только после успешного завершения и полной проверки всего результата. Поэтому после
падения процесса, сервиса или ОС незавершённая папка остаётся явно помеченной.
Восстановление никогда не пишет непосредственно в parent и никогда не использует
существующую папку результата. Target запрещён внутри
backup repository, mirror root и `.backup-system`; это проверяет executor по
фактическим volume/path identities.

Для snapshot `version` равен `latest` либо строго валидированному snapshot ID этого
job repository. При приёме restore-запроса manager немедленно разрешает `latest` в
конкретный snapshot ID и сохраняет ID в queued operation; появление более нового
snapshot во время ожидания не меняет выбранную версию. Для mirror допустим только
`latest`. Snapshot adapter использует
штатный restic restore; mirror копирует требуемый final/subtree и сверяет каждый файл
по catalog SHA-256. Любая ошибка чтения или hash mismatch даёт restore failure, но
не удаляет уже восстановленные файлы: папка результата сохраняется с явным
`.restore-incomplete` marker. При полном успехе marker удаляется, но сама папка
остаётся обычным постоянным результатом восстановления.

После записи всех данных restore переходит в явно видимую фазу `verifying` и
повторно читает все восстановленные файлы с target volume, сверяя содержимое с
выбранной версией backup. На входе в эту фазу CLI печатает путь результата и явное
сообщение: все файлы уже восстановлены и доступны для работы, но итоговая проверка
ещё выполняется. Прогресс записи и прогресс verification отображаются раздельно.
CLI одновременно предупреждает, что до окончания verification файлы можно читать и
копировать, но нельзя изменять, переименовывать или удалять. Обнаруженное изменение
набора файлов или их содержимого делает verification недостоверной и завершает
restore ошибкой; приложение не пытается отличить действие пользователя от иной
внешней модификации.
До успешного окончания verification операция не считается успешной и
`.restore-incomplete` marker не удаляется.

Приложение не имеет retention/TTL/cleanup для результатов restore и никогда не
удаляет их автоматически. CLI выводит фактический путь созданной папки, result и
восстановленный logical size; дальнейшее перемещение или удаление выполняет человек.

До создания папки результата executor по возможности вычисляет logical size
выбранных для восстановления данных и читает свободное место целевого volume. Если
оценка надёжна и свободного места меньше требуемого размера, restore не начинается и
завершается ошибкой preflight. Если надёжно определить размер заранее невозможно,
restore разрешается с warning. Фактическое исчерпание места в процессе всегда
немедленно останавливает операцию; уже записанные данные сохраняются в папке с
`.restore-incomplete` marker.

Restore проходит обычный disk lifecycle и глобальную очередь, но не меняет
`slot_counter`, retention, verification gate или backup freshness. Он разрешён при
verification critical, поскольку может понадобиться извлечение ещё читаемых данных.
Глобальная сериализация распространяется на restore без исключений: пока выполняется
любой backup, restore не начинается; пока выполняется restore, не начинается ни один
backup или другая операция. Одновременно во всей системе существует не более одного
активного executor process.

Принятый manual restore не прерывает active operation, но manager размещает его перед
всеми ожидающими scheduled operations. Поэтому он становится следующей работой после
текущего executor. Scheduled operations из очереди сохраняют взаимный порядок.
Несколько ожидающих manual operations образуют FIFO-группу: новая manual operation
добавляется после уже принятых manual, но перед scheduled-группой. Пересортировка не
меняет их `operation_id`.

Успешный ручной restore не создаёт немедленное Telegram-сообщение и включается в
ближайший суточный heartbeat. Неуспешный restore немедленно создаёт alert с job,
выбранной version, фазой отказа, краткой причиной и фактическим путём сохранённой
папки с `.restore-incomplete` marker.
При глобальном disk-lifecycle safety latch сначала обязателен manual recover.

Отдельно должен существовать документ disaster recovery, достаточный для восстановления на чистой Windows-машине при потере manager, SQLite и конфигурации UI. Для snapshot с `mode: password` независимая копия job-конфига/passphrase обязательна; для `mode: none` секрет восстановления отсутствует.

## 19. Безопасность

- Status UI не имеет управляющих методов.
- Отдельный Status Publisher и HTTP backend отсутствуют.
- Manager не передаёт секреты в status store.
- Backup-диск offline вне окна работ.
- Snapshot repository всегда использует внутренний криптографический формат restic, но password protection является опцией job.
- Snapshot passphrase при наличии защищён ACL job-конфига и никогда не публикуется; Telegram secrets могут защищаться DPAPI отдельно.
- Все subprocess запускаются без shell-интерпретации: Python `subprocess` получает список аргументов.
- Пути и аргументы проходят строгую валидацию.
- Stable root и весь `data` защищены утверждёнными ACL; отдельного privileged helper нет.
- Неизвестный или подменённый диск блокирует backup.
- nginx ограничивает доступ ожидаемыми LAN-подсетями, хотя статические status JSON всё равно считаются публичными внутри LAN.
- Версии Python-приложения и backup-движка фиксируются и отображаются в локальной диагностике.

## 20. Отказоустойчивость

Необходимо протестировать:

- завершение executor во время backup;
- завершение manager во время backup;
- перезагрузку Windows с online-диском;
- исчезновение диска во время чтения/записи;
- недостаток места;
- CRC/read error источника;
- повреждение части репозитория;
- отказ VSS;
- зависший `restic.exe`;
- недоступность Telegram;
- повреждение SQLite;
- недоступность статических status-файлов и перезапуск nginx.

Сбой уведомления или UI не должен превращать успешный backup в failed. Сбой возврата диска offline всегда повышает итог как минимум до warning и создаёт отдельное критическое уведомление.

## 21. Миграция с Cobian

Существующие зеркала не удаляются до выполнения всех условий:

1. Создан новый полный снимок.
2. Выполнена проверка репозитория с чтением данных.
3. Выполнено тестовое восстановление.
4. Проверено восстановление нескольких известных файлов каждого блока.
5. Новый цикл успешно отработал несколько раз по расписанию.
6. Принято отдельное явное решение об освобождении старого места.

Текущие CRC-ошибки на `T:` исследуются до миграции. Следует проверить, сохранились ли читаемые версии проблемных файлов в старом зеркале на `B:`.

При текущей ёмкости `B:` безопасное сосуществование старого зеркала и нового репозитория может оказаться невозможным. Нужен временный или новый backup-диск достаточного объёма.

## 22. Этапы реализации

### Этап 0. Исследование

- классифицировать данные на всех дисках;
- исследовать CRC и состояние физического диска с `F:/S:/T:/U:`;
- определить новый backup-носитель;
- измерить количество и суточную изменчивость данных;
- проверить требования приложений на `S:` к консистентности.

### Этап 1. Proof of concept

- тестовый restic-репозиторий;
- Unicode и длинные пути;
- VSS;
- один тестовый блок;
- online/mount/offline цикл;
- прерывание процесса;
- backup, check и restore-test.

### Этап 2. Executor

- схема конфигурации;
- структурированные события;
- exit codes;
- disk identity guard;
- интеграционные тесты отказов.

### Этап 3. Manager

- Windows Service;
- SQLite;
- очередь;
- scheduler;
- recovery после рестарта;
- Telegram;
- локальный `backupctl`.

### Этап 4. Status UI

- status snapshot;
- статические read-only JSON-контракты;
- периодический polling статуса;
- dashboard;
- nginx-конфигурация;
- редактирование чувствительных данных.

### Этап 5. Миграция

- параллельная работа со старой системой;
- проверенные восстановления;
- наблюдение нескольких циклов;
- отключение Cobian;
- отдельное решение о старых зеркалах.

## 23. Критерии готовности MVP

- Backup запускается по расписанию без интерактивной сессии пользователя.
- Backup-диск находится offline до и после работы.
- Подмена или изменение идентичности диска блокирует запись.
- Каждый утверждённый блок успешно защищается согласно своему `job.kind`: `snapshot` или `mirror`.
- Открытые файлы обрабатываются через VSS либо явно классифицируются как неподдержанные.
- Применяется утверждённая retention policy.
- Проверка репозитория выполняется автоматически.
- Ручной restore-test реализован; обязательный post-launch запуск назначен примерно
  через месяц и его результат отображается в истории.
- После аварийного завершения состояние корректно восстанавливается.
- Telegram сообщает об успехах и проблемах.
- Status UI доступен через nginx в LAN и технически не способен управлять системой.
- Система восстанавливается по документации на чистой машине.

## 24. Deployment inputs, ещё не определённые для конкретной машины

1. Какой физический диск станет новым backup-носителем?
2. Нужна ли физическая коммутация питания диска в будущем?
3. Что находится на `S:` и можно ли защищать эти данные обычным VSS file backup;
   данные, требующие application-specific hooks, не входят в v1.0?
4. Нужно ли резервировать Windows `C:` целиком или достаточно конфигураций приложений?
5. Какие данные на `I:`, `M:` и `V:` являются восстанавливаемыми и какие расходными?
6. Какой допустимый RPO для каждого блока?
7. Какой допустимый срок полного восстановления (RTO)?
8. Нужен ли второй, физически вынесенный репозиторий?
9. Где оператор хранит независимую копию passphrase, если password-mode когда-либо
   будет включён, и disaster recovery инструкции?
10. Какой LAN URL и существующий nginx instance будут использованы?

Эти пункты не меняют архитектуру v1 и заполняются при инвентаризации и создании
production-конфигов.

## 25. Оставшиеся deployment-решения

- новый физический носитель;
- состав job по реальным блокам данных, их kind, cron, RPO и retention;
- nginx URL и filesystem aliases;
- Telegram credentials;
- необходимость второго физически вынесенного repository в будущем.

## 26. Нормативный контракт v1

Слова «должен», «не должен» и «запрещено» в разделах 26–38 являются требованиями реализации v1. Если реализация не может выполнить требование, изменение сначала оформляется в ADR и только затем в коде.

### 26.1. Инварианты границ

- Manager не читает и не интерпретирует операционные конфиги executor.
- Manager никогда не импортирует Python-пакет executor.
- Executor не подключается к SQLite manager.
- Executor не отправляет Telegram-сообщения и не публикует web-status.
- UI и nginx читают только immutable `web` assets и `data\public` projections.
- Manager никогда не читает входные данные из каталога статической публикации.
- Lifecycle backup-диска принадлежит только executor.
- Один процесс executor обслуживает ровно одну операцию одного `job_id`.
- В v1 одновременно работает не более одного executor независимо от числа дисков и репозиториев.
- Любая операция executor заканчивается попыткой вернуть затронутый backup-диск offline.
- Наличие созданного restic snapshot не маскирует ошибку возврата диска offline.

### 26.2. Источники истины

| Данные | Источник истины |
|---|---|
| Расписание и уведомления | `data/config/manager.yaml` |
| Параметры выполнения backup | `data/config/jobs/<job-id>.yaml` |
| История оркестрации | SQLite manager |
| Наличие и содержимое backup | restic repository |
| Текущий публичный статус | атомарные JSON-проекции |
| Snapshot passphrase | защищённый ACL executor job config; отсутствует при `mode: none` |
| Telegram secrets | manager secret storage/DPAPI |

SQLite и публичные JSON-файлы не используются для доказательства существования конкретного файла в backup.

## 27. Структура исходного репозитория

```text
backup-system/
├── pyproject.toml
├── uv.lock
├── README.md
├── LICENSES/
├── config.example/
│   ├── manager.yaml
│   └── jobs/
│       └── example.yaml
├── src/backup_system/
│   ├── common/
│   │   ├── events.py
│   │   ├── ids.py
│   │   ├── json_io.py
│   │   └── time.py
│   ├── executor/
│   │   ├── cli.py
│   │   ├── config.py
│   │   ├── disk_control.py
│   │   ├── lifecycle.py
│   │   ├── adapters/
│   │   │   ├── base.py
│   │   │   ├── snapshot_restic.py
│   │   │   └── mirror.py
│   │   ├── secrets.py
│   │   └── restore_test.py
│   ├── manager/
│   │   ├── service.py
│   │   ├── scheduler.py
│   │   ├── queue.py
│   │   ├── runner.py
│   │   ├── database.py
│   │   ├── commands.py
│   │   ├── projections.py
│   │   └── telegram.py
│   ├── ctl/
│   │   └── cli.py
│   └── status_ui/
│       ├── index.html
│       ├── app.js
│       └── style.css
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   └── windows/
├── packaging/
│   ├── windows-service/
│   └── nginx/
└── docs/
    ├── ADR/
    ├── OPERATIONS.md
    └── DISASTER_RECOVERY.md
```

В production используется один Python package с разными entry points:

```text
backup-manager
backup-executor
backupctl
```

Это не означает общую бизнес-логику manager и executor: общими являются только типы событий, UUID, время и безопасная JSON-сериализация.

## 28. Runtime layout Windows

Production использует один отдельный Stable root рядом со всеми данными приложения.
Конкретный абсолютный путь выбирается при развёртывании; ниже только пример:

```text
C:\BackupSystem\Stable\
    backup-system.root
    app\
    .venv\
    bin\
        restic.exe
        smartctl.exe
    web\
    data\
        config\
            manager.yaml
            jobs\
        state\
            manager.sqlite3
            executor\
                <job-id>.json
        commands\
            incoming\
            accepted\
            completed\
            rejected\
        logs\
        public\
            status.json
            health.json
            logs\
        temp\
```

ACL:

- весь Stable root: запись только Administrators/System;
- `data` — единственный каталог изменяемых application data;
- `data\config`, `data\state`, `data\logs`, `data\temp`: доступ только
  Administrators/System;
- `data\commands\incoming`: запись Administrators/System, чтение manager;
- `data\public`: запись только manager, чтение nginx service account;
- nginx не имеет доступа к остальным подкаталогам `data`.

Root не задаётся через `manager.yaml`. Entry points находят ближайший родительский
каталог `.venv`, содержащий обязательный marker `backup-system.root`; NSSM запускает
interpreter именно из `<root>\.venv`. Поставочные пути (`app`, `.venv`, `bin`, `web`)
и все рабочие пути под единым `<root>\data` имеют фиксированное расположение.
Отсутствующий/неоднозначный marker является fatal
bootstrap error. Это не делает source/destination backup paths относительными: они
по-прежнему должны быть абсолютными локальными Windows paths.

`data` не является частью поставки приложения и никогда не очищается, не заменяется
и не копируется из Dev при deploy. Config, SQLite, queue/spool, logs, public
projections и temp принадлежат именно этому рабочему каталогу. Temporary artifacts
могут очищаться только своей документированной runtime-логикой, но не deployment.

Скрытого или привилегированного self-backup рабочего каталога нет. Если конфигурации
и disaster-recovery документация должны архивироваться, для них создаётся обычная
configured backup job по тем же правилам, что и для пользовательских данных. В её
source включаются `data\config` и recovery-документы; `data\state`, `commands`,
`logs`, `public` и `temp` архивной ценности не имеют и исключаются. Потеря SQLite
обрабатывается уже определённой процедурой state recovery, а не восстановлением
живой manager database из файловой копии.

Для snapshot job с `encryption.mode: password` backup job-конфига внутрь того же
зашифрованного repository не считается независимым сохранением ключа: без passphrase
такую копию невозможно извлечь. Оператор обязан отдельно сохранить passphrase вне
этого repository. v1 только документирует это требование и не имеет key escrow,
проверки наличия внешней копии, confirmation state или автоматического управления
ключами.

Mount point backup-диска размещается отдельно, например `C:\BackupVolumes\primary`. Конкретный путь является частью executor config и не публикуется.

## 29. Конфигурация v1

Формат v1 — UTF-8 YAML без BOM. Загрузка выполняется `yaml.safe_load`, затем строгой Pydantic v2-моделью с `extra='forbid'`. Неизвестное поле является ошибкой. Относительные пути запрещены.

Manager config читается и валидируется при startup; его hot reload отсутствует и для
изменения расписания/уведомлений требуется service restart. Executor job configs не
имеют generation/hash/change tracking. При startup manager безусловно вызывает
`backup-executor validate` для всех configured jobs, чтобы показать исходные ошибки,
но затем перед каждой фактической operation новый executor заново читает текущий
job YAML и строго валидирует его до disk/VSS действий.

Если job config был изменён после startup и остаётся валидным, executor просто
использует его текущее содержимое. Если чтение или валидация не удались, data work не
начинается, operation получает `config_invalid`, cycle не продвигается, UI показывает
ошибку, а Telegram отправляет alert по обычным правилам failed job. Manager не
отслеживает сам факт изменения и не вводит состояния `config_changed` или
`restart_required`. Доступен отдельный локальный validation-only запуск, который
проверяет manager config и все executor job configs без постановки backup operation в
очередь и без disk lifecycle.

Невалидный или нечитаемый manager config является fatal startup error: process
завершается отдельным документированным exit code до scheduler/queue initialization.
Для этого exit code NSSM настраивается на `Exit`, а не `Restart`, чтобы постоянная
ошибка конфигурации не создавала restart loop. После исправления оператор запускает
service явно.

До успешного чтения manager config система не полагается на Telegram, SQLite или
Web projection. Fatal bootstrap diagnostic в UTF-8 выводится в stderr для захвата
NSSM и дублируется одной структурированной JSONL-записью в ACL-защищённый bootstrap
log `<root>\data\logs\bootstrap.jsonl`, после чего выполняются flush и
`FlushFileBuffers`. Отдельный Windows Event Log integration и специальный аварийный
notification channel в v1 не создаются.

Ошибка startup validation одного executor job config не мешает запуску manager и
других jobs. Такая job получает health `config_invalid`, исключается из cron triggers
на время данного manager process и перечисляется в одном startup Telegram alert.
После исправления файла требуется service restart: новая startup validation вернёт
job в scheduler. Если config стал невалидным уже после успешного startup, ближайший
executor обнаружит это перед disk lifecycle; failed phase не продвинет cycle.

### 29.1. Manager config

```yaml
schema_version: 1
timezone: Europe/Samara

scheduler:
  poll_seconds: 5

monitoring:
  volumes:
    poll_seconds: 60
    items:
      - id: system
        display_name: System
        volume_guid: '<configured-volume-guid>'
      - id: backup-primary
        display_name: Backup primary
        volume_guid: '<configured-volume-guid>'
jobs:
  - id: data
    enabled: true
    display_name: Data
    schedule:
      cron: '0 0 * * 1'
      timezone: Europe/Samara
      deadline: '08:00'
      cycle:
        - {operation: backup}
        - {operation: backup}
        - {operation: backup}
        - {operation: backup}
        - {operation: check, mode: subset}

telegram:
  enabled: true
  token_secret: telegram-bot-token
  chat_id_secret: telegram-chat-id
  daily_report_cron: '0 9 * * *'  # только пример; значение выбирает оператор
  daily_report_timezone: Europe/Samara
  stale_manager_minutes: 10

```

Manager валидирует только эту схему. Для каждого enabled `job.id` он отдельно запускает `backup-executor validate --job <id>` и сохраняет результат проверки.

Cron трактуется в указанной IANA timezone. Для одного совпавшего момента создаётся
не более одной операции. Поле `cycle` — непустой упорядоченный список операций;
типичный snapshot-job использует `[backup, backup, backup, backup, check]`. Manager
хранит монотонный `slot_counter` в SQLite. Операция планового слота вычисляется как
`cycle[slot_counter % len(cycle)]`. Счётчик увеличивается только после terminal
`success` или `warning` плановой operation.

Catch-up отсутствует: если manager не работал в момент cron-срабатывания, после
запуска он не создаёт пропущенную operation. Любой пропущенный слот, независимо от
operation kind, не продвигает `slot_counter`: в следующий штатный cron job снова
выполняет ту же фазу. Manager сохраняет `schedule_missed` с operation, которая
должна была выполняться.

Failed, cancelled, interrupted или inconclusive scheduled operation любого kind не
продвигает цикл. В следующий cron-слот той же job повторяется та же фаза. Для failed
check это означает, что новые backup этой job не запускаются до успешной проверки;
для failed backup, включая ошибку retention, следующий слот снова выполняет полный
`new snapshot → retention`. Это ожидание не создаёт дополнительных operations между
cron-срабатываниями.

Terminal `warning` продвигает цикл, поскольку обязательный результат operation
получен, а замечание является нефатальным. Для `check` warning допустим только если
проверка требуемого объёма завершилась успешно (например, одновременно произошёл
безопасный `scrub_cursor_reset`); любое неподтверждённое состояние check
классифицируется как `failed/critical`, а не warning.

Успешный ручной check разблокирует цикл, если текущая scheduled-фаза этой job —
`check` и ручная операция проверила как минимум требуемую текущую subset-часть либо
весь repository. В этом случае manager один раз продвигает `slot_counter`, и
следующей scheduled operation становится backup. Неуспешный или только metadata
manual check цикл не меняет. Ручные backup и restore-test `slot_counter` не меняют.

Пока цикл зафиксирован на failed check, snapshots и retention этой job не меняются:
retention является стадией только реально выполняемого backup, а scheduler не
подменяет обязательную проверку новым backup-run.
Изменение массива `cycle` в конфиге не сбрасывает `slot_counter`, не создаёт run и
не перестраивает прошлые слоты; следующая operation вычисляется по новому массиву и
сохранённому счётчику.

При старте manager собирает все достоверно установленные `schedule_missed` за
прошедший простой в один компактный Telegram startup report. Детализация зависит от
важности:

- пропущенные `backup` перечисляются поимённо с плановым временем;
- пропущенные `check` перечисляются поимённо с плановым временем;
- `prune` и прочие служебные job operations сворачиваются в
  одну строку с общим количеством;
- прерванный предыдущий run и неподтверждённое состояние backup-диска всегда
  выводятся отдельными строками перед списком пропусков;
- если важные operations не пропущены, отчёт явно сообщает об этом одной строкой.

Отчёт также содержит длительность простоя manager. Отдельное Telegram-сообщение на
каждый пропуск не отправляется; startup report ничего не ставит в очередь.

Для сочетания `job_id + operation kind` допускается не более одной незавершённой
operation в состояниях `queued` или `running`. Если очередное cron-срабатывание
приходит, когда такая operation уже выполняется или ожидает в очереди, новая запись
не создаётся. Manager сохраняет событие `duplicate_trigger_skipped` с временем
срабатывания и причиной `already_running` либо `already_queued`.

### 29.2. Executor job config

```yaml
schema_version: 1
id: data
kind: snapshot
display_name: Data

source:
  path: 'F:\'

excludes:
  - 'System Volume Information'
  - '$RECYCLE.BIN'

repository:
  engine: restic
  repository_id: primary
  path: 'C:\BackupVolumes\primary\restic'
  marker_uuid: '<uuid4>'
  encryption:
    mode: none
  marker_file: 'C:\BackupVolumes\primary\.backup-volume.json'

disk:
  physical_serial: '<normalized-serial>'
  expected_size_bytes: 4000787030016
  partition_guid: '<guid>'
  volume_guid: '<guid>'
  repository_path_timeout_seconds: 30

backup:
  host: basovs-server
  tags: ['job:data']
  read_error_result: failed

retention:
  keep_last: 1
  keep_daily: 0
  keep_weekly: 4
  keep_monthly: 6
  keep_yearly: 0

verification:
  data_subset_parts: 4
  restore_test_paths:
    - 'F:\<control-file-relative-path>'
```

Форма `mirror` job использует те же `source`, `excludes` и `disk`, но вместо
`repository`, `backup` и `retention` содержит один destination:

```yaml
schema_version: 1
id: data-mirror
kind: mirror
display_name: Data mirror
source:
  path: 'F:\'
excludes: []
destination:
  path: 'C:\BackupVolumes\primary\mirrors\data'
  marker_file: 'C:\BackupVolumes\primary\mirrors\data\.backup-system\marker.json'
  marker_uuid: '<uuid4>'
disk:
  physical_serial: '<normalized-serial>'
  expected_size_bytes: 4000787030016
  partition_guid: '<guid>'
  volume_guid: '<guid>'
  repository_path_timeout_seconds: 30
verification:
  restore_test_paths:
    - 'F:\<control-file-relative-path>'
```

Форма repository-wide `maintenance` job не имеет source/excludes и явно ссылается
на единственную owning snapshot job. Поля `repository` и `disk` обязаны в точности
совпадать с owner; executor загружает оба job configs и проверяет это до disk
lifecycle:

```yaml
schema_version: 1
id: data-maintenance
kind: maintenance
display_name: Data repository maintenance
repository_owner_job_id: data
repository:
  engine: restic
  repository_id: primary
  path: 'C:\BackupVolumes\primary\restic'
  marker_uuid: '<same-uuid-as-owner>'
  encryption:
    mode: none
  marker_file: 'C:\BackupVolumes\primary\.backup-volume.json'
disk:
  physical_serial: '<same-serial-as-owner>'
  expected_size_bytes: 4000787030016
  partition_guid: '<same-guid-as-owner>'
  volume_guid: '<same-guid-as-owner>'
  repository_path_timeout_seconds: 30
```

Один `job_id` соответствует одному файлу `jobs/<job-id>.yaml`; дополнительные пути и поиск по glob запрещены. Job ID соответствует регулярному выражению `^[a-z][a-z0-9-]{0,62}$`.

`excludes` v1 — список точных относительных путей внутри единственного source root.
Каждая запись исключает указанный объект и всех его descendants. Запрещены:

- абсолютные пути, drive letters, leading separator и `..`;
- glob/wildcard (`*`, `?`, `**`), regex и negation;
- правила «по basename в любом месте дерева»;
- дубли и case-insensitive path collisions.

Пути канонизируются по тем же Windows case-insensitive rules, что и mirror catalog.
Executor генерирует из этого списка engine-specific параметры restic и применяет ту
же проверку prefix/components в mirror; обе реализации обязаны пройти общие contract
fixtures. Наличие configured exclude является штатным поведением и не создаёт
warning/error.

Job-конфиг является discriminated union по полю `kind`. Состав v1 зафиксирован:

- `snapshot` — версионный дедуплицированный backup через restic; имеет `source`,
  `backup`, `retention`, `verification` и поддерживает
  scheduled `backup|check` и manual `restore|restore-test|recover`; retention является
  стадией `backup`, а не отдельной operation;
- `mirror` — точная файловая реплика текущего состояния source; поддерживает
  scheduled `backup|check` и manual
  `restore|restore-test|repair-mirror|recover`;
- `maintenance` — не имеет `source`, ссылается на тот же `repository`/`disk` и
  поддерживает scheduled `prune` и manual `recover`.

Каждая enabled snapshot job владеет отдельным restic repository. Две snapshot jobs
не могут указывать один repository path, marker UUID или repository ID. Единственное
разрешённое совместное использование — одна связанная maintenance job того же
владельца, поддерживающая только `prune|recover`; check выполняется циклом самой
snapshot job.

Executor отклоняет операцию, не разрешённую для `kind`. Manager намеренно не дублирует эту таблицу: ошибочная пара `job/operation` завершается `config_invalid` до изменения состояния диска.

Каждая `snapshot` и `mirror` job v1 требует ровно один source root на одном
подтверждённом NTFS volume и читает его через один VSS snapshot. Несколько source
roots, aliases, объединение деревьев и межтомовая point-in-time консистентность не
поддерживаются; данные оформляются отдельными независимыми jobs. `mirror`
дополнительно имеет ровно один destination root, relative tree которого соответствует
source root один-к-одному. Нарушение ограничения является `config_invalid`.

VSS обязателен и не является config option. До запуска adapter executor создаёт и
проверяет VSS snapshot source volume; live-source path в data operation никогда не
передаётся. Ошибка VSS завершает run до открытия destination/repository для изменения.
Snapshot adapter передаёт restic уже проверенный shadow-root и не включает
restic-owned filesystem snapshot/VSS options. Поэтому snapshot и mirror используют
один executor VSS lifecycle, а двойное создание snapshots запрещено.

Оба data adapters представляют содержимое как логическое дерево относительно
configured source root. Его корень имеет имя `.`, а catalog/repository paths являются
только нормализованными relative paths ниже него. Drive letter, volume GUID,
фактический mount point и технический `HarddiskVolumeShadowCopyN` запрещено включать
в backup namespace. Executor вычисляет относительное положение source root на
volume, применяет его к shadow-root и передаёт adapter-у стабильный logical root.
Restic PoC обязан доказать, что list/restore показывают это относительное дерево и не
сохраняют VSS device prefix.

VSS реализуется отдельным executor-owned Python adapter поверх структурированного
Windows VSS API/COM. Запрещены PowerShell, `wmic`, `diskshadow`, shell и разбор
локализованного console output. Adapter принимает подтверждённую identity source
volume, создаёт snapshot, получает snapshot ID и shadow device path, проверяет их
принадлежность ожидаемому volume и возвращает только провалидированный shadow-root
data adapter-у. Snapshot ID остаётся локальной диагностикой. Освобождение snapshot
всегда выполняется в executor `finally`; ошибки API сохраняются как HRESULT и
нормализованный error class.

v1 использует client-accessible filesystem snapshot без application-specific VSS
Writer coordination. Контракт покрывает обычные файлы, включая открытые локально или
через SMB, но не обещает application-consistent backup SQL Server, Exchange и иных
систем, которым нужен writer-aware transaction quiescing. Такие источники запрещено
маскировать под обычную file job; для них потребуется отдельный типизированный adapter
и ADR.

VSS cleanup использует доказуемое владение. После `StartSnapshotSet` и до
`AddToSnapshotSet`/`DoSnapshotSet` executor атомарно сохраняет intent с run ID,
source volume identity и полученным `SnapshotSetID` в
`data\state\executor\<job-id>.json`, flush-ит файл и вызывает `FlushFileBuffers`.
Только после этого разрешено фактическое создание snapshot. После успешного delete
intent помечается cleaned/удаляется durable update-ом.

Startup/recover никогда не удаляет VSS snapshot по возрасту, source volume или
эвристике. Разрешено удалять только exact SnapshotSetID из незавершённого собственного
intent. Если set уже отсутствует, cleanup считается идемпотентно успешным. Если
принадлежность доказать нельзя, snapshot не трогается, состояние остаётся
warning/critical для ручного исследования. `backupctl recover <job-id>` включает
cleanup принадлежащего job VSS intent до подтверждения disk offline.

Произвольные pre/post scripts, hooks, shell/PowerShell commands и executable paths в
job config запрещены в v1.0. Backup приложений, которым недостаточно filesystem VSS
snapshot, требует будущего отдельного типизированного adapter и ADR; такие данные не
включаются в v1 job под видом обычных файлов.

### 29.3. Глобальный SMART allowlist executor

`config/smart.yaml` читается executor, но не интерпретируется manager:

```yaml
schema_version: 1
per_disk_timeout_seconds: 30
stale_after_hours: 48
disks:
  - id: source-main
    display_name: Main source disk
    identity:
      serial: '<normalized-serial>'
      expected_size_bytes: 8001563222016
  - id: backup-primary
    display_name: Primary backup disk
    identity:
      serial: '<normalized-serial>'
      expected_size_bytes: 12000138625024
```

Allowlist содержит все постоянные физические диски сервера, которые должны
появляться в SMART dashboard, независимо от участия в текущей job. Новые/внешние
диски автоматически не добавляются. Отсутствующий configured диск сохраняет
последнее наблюдение с `stale/unknown` и не создаёт backup failure. Serial и другие
hardware identifiers используются только локально и не публикуются.

### 29.4. Marker file

Marker создаётся только provisioning-командой, не штатным executor:

```json
{
  "schema_version": 1,
  "repository_id": "primary",
  "marker_uuid": "<uuid4>",
  "created_at": "<rfc3339>"
}
```

Executor сверяет UUID marker с executor config. Несовпадение или отсутствие marker запрещает открытие restic repository на запись.

## 30. Алгоритм manager

### 30.1. Startup

1. Получить single-instance lock manager.
2. Загрузить и строго провалидировать `manager.yaml`.
3. Открыть SQLite, включить foreign keys и выполнить миграции.
4. Найти runs со статусом `running`; отметить `interrupted`.
5. Выполнить `executor validate-smart-config`, затем для каждого enabled job —
   `executor validate`, с коротким timeout.
6. Рассчитать только будущие срабатывания расписаний; пропущенные во время простоя
   operations не создавать.
7. Отправить один startup report со всеми установленными пропусками за время
   простоя.
8. Обработать незавершённые spool-команды идемпотентно.
9. Опубликовать initial JSON projection.
10. Войти в основной loop.

Manager при старте не переводит диски online и не ставит recovery автоматически.
Если предыдущий run завершился на disk-owning стадии без
`disk_offline_confirmed`, устанавливается глобальный safety latch; разрешена только
ручная `backupctl recover <job-id>` после расследования оператором.

### 30.2. Main loop

На каждой итерации:

1. Обновить heartbeat.
2. Принять и провалидировать локальные spool-команды.
3. Для наступивших scheduled operations проверить отсутствие такой же job/operation
   в `queued` или `running`; уникальные поставить в SQLite queue, дубли записать как
   `duplicate_trigger_skipped`.
4. Если установлен disk-lifecycle safety latch, не запускать queued operations;
   исключение — явно принятая ручная `recover` для job, создавшей latch.
5. Если executor не работает, выбрать старейшую runnable operation.
6. Создать run UUID и транзакционно отметить operation `running`.
7. Запустить executor без shell и читать stdout/stderr асинхронно.
8. Валидные события записывать в SQLite; невалидную строку сохранить как diagnostics и повысить warning.
9. Классифицировать результат по terminal event и exit code.
10. Отправить Telegram и атомарно обновить JSON projection.

### 30.3. Идемпотентность

Scheduled operation имеет ключ:

```text
schedule:<job-id>:<operation-kind>:<scheduled-rfc3339>
```

Manual operation имеет UUID команды. Уникальный индекс SQLite запрещает повторный
запуск одной логической операции после рестарта manager. Для `run` дополнительно
действует ограничение одной unfinished operation на `job_id + kind`: команда либо
создаёт её первой в ожидающей очереди, либо поднимает уже ожидающую operation, либо
становится no-op для уже выполняющейся.

### 30.4. Штатная остановка Windows Service

Отдельного scheduler pause-state нет. При получении SCM stop manager:

1. перестаёт принимать cron triggers и новые spool-команды;
2. транзакционно переводит все `queued` operations в `cancelled` с причиной
   `service_stopping`; их cycle phase не продвигается;
3. если executor работает, посылает ему cooperative cancel;
4. продолжает принимать события, пока executor выполняет adapter cleanup и возврат
   backup-диска offline;
5. публикует финальный status snapshot, закрывает SQLite и завершает service.

Старые queued operations после следующего старта не возобновляются, catch-up не
выполняется; jobs ждут будущих cron-срабатываний и повторяют непройденную фазу.
Manager не применяет автоматический hard kill по времени. Пока cooperative cancel не
завершён, service остаётся `STOP_PENDING` с корректным SCM wait hint. Если процесс
действительно завис, оператор исследует и завершает нужный process средствами
Windows; последующий startup применяет обычные `interrupted`/disk recovery rules.

## 31. Алгоритм executor

Для `run`:

```text
load strict job config
emit run_started
acquire machine-wide executor lock
inspect configured physical disk while offline
verify hardware identity
bring disk online
emit disk_online
try:
    ensure repository path exists
    verify volume GUID and marker UUID
    collect SMART for configured physical-disk allowlist
    resolve repository secret
    select adapter strictly by job.kind
    run adapter operation
    normalize adapter output into common executor events
    determine operation result
finally:
    terminate owned child processes
    release repository handles
    take configured physical disk offline
    verify offline state with bounded retries
    emit disk_offline_confirmed or disk_offline_failed
emit exactly one run_finished
exit with normalized code
```

Требования:

- Lock создаётся до изменения состояния диска.
- Если identity не подтверждена, `bring_online` не вызывается.
- После online повторно проверяются volume GUID и marker UUID.
- Нельзя считать ранее сохранённый номер Windows disk number устойчивым идентификатором.
- Все отдельные hardware/API ожидания lifecycle имеют bounded timeout; общая
  длительность backup/check и ожидание cooperative service-stop timeout не имеют.
- `finally` не подавляет исходную ошибку, но failure вернуть диск offline имеет более высокий severity.
- Сигналы остановки manager приводят к прекращению restic и выполнению cleanup.
- Executor не продолжает следующий job: один процесс — одна операция.

Если обязательный data operation уже успешно завершён, но удалить принадлежащий run
VSS snapshot не удалось, data result остаётся `success`; отдельно создаётся system
health `warning` `vss_cleanup_failed` и немедленный Telegram alert. Run сохраняет
локальный VSS identifier и volume identity для диагностики, но device/shadow path не
публикуется в LAN или Telegram. Ошибка cleanup не отменяет попытку вернуть
backup-диск offline. Повторный cleanup выполняется только при следующем executor
startup/recovery либо вручную оператором; manager сам VSS не управляет.

Для `check`, `prune`, `restore-test` и `recover` используется тот же принцип владения
lifecycle. `recover` не изменяет backup-данные: он проверяет состояние разрешённого
физического диска и возвращает его offline, если предыдущий executor не завершил
cleanup.

Только manual recover, завершившийся событием `disk_offline_confirmed`, позволяет
manager транзакционно закрыть соответствующий safety latch и продолжить сохранённую
FIFO-очередь. Failed/inconclusive recover latch не снимает. Обычный успешный backup,
рестарт manager или истечение времени также не снимают latch.

## 32. Backup adapters v1

### 32.1. Общий контракт adapter

Каждый adapter реализует одинаковую внутреннюю границу:

```text
validate(config)
backup(context)
check(context, mode)
restore(context, request)
restore_test(context)
repair_mirror(context)    # только mirror
cancel(context)
```

Adapter не управляет физическим диском, scheduler, SQLite, Telegram или публичным status. Он получает уже проверенный доступный destination от executor lifecycle и возвращает нормализованные события/результат.

`repair-mirror` доступен только при активном verification gate mirror job. Это одна
неделимая с точки зрения результата operation:

1. создать VSS source;
2. выполнить обычные scan/plan/preflight и привести mirror/catalog к source;
3. выполнить полный metadata scan destination;
4. прочитать и сверить SHA-256 всех `present` entries;
5. только при общем terminal success снять verification gate и один раз продвинуть
   зафиксированную check-фазу цикла.

Промежуточный успех синхронизации gate не снимает. Ошибка reconciliation или full
check оставляет job `critical`, cycle на check и следующий scheduled trigger снова
check. Отдельный manual check после успешного `repair-mirror` не требуется.

### 32.2. Snapshot/restic adapter

Restic вызывается нативно на Windows с `creationflags` для отдельной process group и без `shell=True`.

#### 32.2.1. Rationale optional password protection

Шифрование не является самостоятельной целью продукта v1. Поддержка `mode: password` включена только потому, что restic в любом случае использует криптографический repository format, а password protection предоставляется самим bundled engine практически без дополнительной data-path логики.

Два режима являются равноправными:

- `none` — restic repository с пустым паролем; каждая команда получает обязательный `--insecure-no-password`; секретов и риска потери ключа нет;
- `password` — каждая команда получает `--password-file <temporary-file>`; passphrase берётся из защищённого job-конфига.

Приложение не реализует криптографию, key derivation, rotation или собственный key store для snapshot. Опция не должна усложнять mirror adapter, disk lifecycle, scheduler или UI. UI публикует только `password_protected: true|false`.

Provisioning и все последующие команды одного repository обязаны использовать один режим. Несовпадение режима классифицируется как `repository_auth_mode_mismatch`, а неверный passphrase — `repository_key_invalid`; fallback между режимами запрещён.

Backup-команда логически эквивалентна:

```text
restic --repository <repo> <auth-args>
  backup --json --use-fs-snapshot
  --host <configured-host>
  --tag job:<job-id>
  --exclude-file <generated-exclude-file>
  <source-root>
```

Где `<auth-args>` равно `--insecure-no-password` либо `--password-file <temp-secret-file>` согласно config. То же правило применяется к `forget`, `check`, `prune`, `restore`, `snapshots` и другим restic operations.

Snapshot adapter обязан обеспечить fail-fast поверх restic. Он асинхронно читает
stdout и stderr. Основным контрактом являются structured JSON events. Для точной
поддерживаемой major/minor версии restic дополнительно разрешены только явно
зафиксированные в принятом ADR и покрытые integration-тестами stderr diagnostics.
При первом событии или diagnostic, однозначно классифицированном как source read
error, repository write/I/O error или out-of-space, adapter немедленно инициирует
cooperative termination restic и переходит к cleanup/offline. Он не ждёт окончания
полного обхода source. Такой run получает `failed`; retention не запускается, даже
если restic успел записать частичный snapshot.

PoC обязан подтвердить, что закреплённая версия restic выдаёт эти классы ошибок в
машинно-разбираемом виде достаточно рано. Текстовый классификатор обязан сопоставлять
точные проверенные diagnostics и блокирует обновление restic до повторного
compatibility test; общий поиск подстрок и локализованный shell output запрещены. Если
надёжная ранняя классификация невозможна, snapshot adapter не считается готовым:
молча ослаблять fail-fast до «дождаться exit code после полного обхода» запрещено.

После успешного создания snapshot тот же backup-run применяет retention, ограничивая
выборку tag и host конкретной job:

```text
restic forget --host <configured-host> --tag job:<job-id>
  --keep-last 1 --keep-daily 0 --keep-weekly 4 --keep-monthly 6 --keep-yearly 0
```

Значение по умолчанию для snapshot job — последняя копия, четыре недельных и шесть
месячных. При типичном еженедельном расписании это даёт до 11 различных restore
points и горизонт около полугода; совпадающий snapshot учитывается один раз.
Ежедневный и годовой tiers по умолчанию отключены. Политика является частью
executor job config и может быть явно переопределена для конкретной job.

До успешного создания нового snapshot retention не запускается. Освобождение
pack-файлов (`prune`) является отдельной operation. Для него создаётся отдельный
executor job config обслуживания того же репозитория без source. Оно не запускается
после каждого backup.

Для каждого restic repository допускается ровно одна enabled maintenance job,
выполняющая `prune` по собственному cron (типичный config — раз в месяц в выделенную
ночь). Автоматические triggers по repository size, free-space threshold или сразу
после backup запрещены в v1. Конкретное расписание является только конфигурацией, а
не логикой scheduler.

Неуспешный `prune` сам по себе даёт обслуживающей job и общему system health
`warning`, а не `critical`: операция оптимизирует физическое хранение и не создаёт
точку восстановления. Это не ослабляет правила проверки — последующий failed или
inconclusive `check` того же репозитория немедленно повышает состояние до `critical`.

Retention является обязательной частью snapshot-backup. Если новый snapshot создан,
но `forget` не завершился успешно, весь run получает `failed`, а job health —
`critical`: целевое состояние набора snapshots не подтверждено и операция выполнена
не полностью. При этом executor сохраняет ID созданного snapshot и фактически
наблюдаемый список snapshots для диагностики; он не объявляет сам snapshot
повреждённым без результата проверки. Critical снимается только после успешных
диагностических действий, успешного применения retention и успешного `check`.

Следующий cron-запуск после failed retention не переходит в специальный repair-mode
и не выполняет retention отдельно. Он проходит обычный линейный алгоритм: создаёт
новый snapshot, затем применяет актуальную retention policy. Это может временно
добавить ещё один snapshot, но сохраняет единый и проверяемый путь выполнения.

Проверки:

- `metadata`: `restic check`;
- `subset`: `restic check --read-data-subset=<part>/4`;
- `full`: `restic check --read-data`.

Команды `restic repair index|packs|snapshots` приложение v1 не выбирает и не
запускает автоматически или через `backupctl`. При failed/inconclusive snapshot
check verification gate блокирует новые backups. Оператор сохраняет диагностический
вывод, при возможности делает защитную копию repository и вручную применяет
подходящую штатную процедуру restic. После неё обязательный
`backupctl check <job-id> --mode full` является единственным способом снять gate и
продвинуть check-фазу.

Дефолтный scheduled check использует четыре детерминированные части. Каждый его
запуск выполняет полную структурную проверку repository и читает очередную четверть
pack-файлов: `1/4`, `2/4`, `3/4`, `4/4`, затем цикл повторяется. При schedule
`backup × 4 → check` и недельном cron полный data scrub занимает 20 недель, то есть
завершается с запасом до шестимесячной границы retention. Случайный процент для
этой политики запрещён, поскольку он не гарантирует полного покрытия.

Executor сохраняет номер проверенной части, начало текущего scrub-cycle и время
последнего завершённого полного круга. UI различает успех отдельной части и полное
покрытие repository. Основание механизма — документированный restic selector
`--read-data-subset=n/t`: https://restic.readthedocs.io/en/stable/045_working_with_repos.html#checking-integrity-and-consistency

Scrub cursor является внутренним состоянием executor и атомарно хранится в
`<root>\data\state\executor\<job-id>.json`; manager получает только нормализованные observation
events для SQLite/UI и не вычисляет аргументы restic. Cursor продвигается с `n` на
`n+1` только после terminal success проверки текущей части. Failed, cancelled,
interrupted или inconclusive check не меняет cursor, поэтому следующий scheduled
check повторяет ту же часть. После успешной `4/4` фиксируется завершение полного
scrub-cycle и cursor возвращается к `1/4`.

Если state-файл cursor отсутствует, не проходит schema/checksum validation или
содержит невозможное значение, executor не пытается угадывать прежнюю фазу. Он
архивирует доступный повреждённый файл в защищённые diagnostics, атомарно создаёт
новое состояние с `1/4` и выполняет эту часть. Run получает как минимум `warning`
`scrub_cursor_reset`, даже если сама проверка успешна. Дата прежнего полного scrub
не публикуется как подтверждённая; новый полный круг считается завершённым только
после последовательных успешных `1/4`–`4/4` нового состояния.

Точные поддерживаемые флаги и exit codes фиксируются integration-тестом против закреплённой версии restic до завершения PoC. Adapter принимает только ожидаемую major/minor version и отказывается работать с неподтверждённой версией, пока compatibility test не обновлён.

### 32.3. Mirror adapter

После успешного `mirror backup` destination должен точно соответствовать выбранному source с учётом excludes. Новые и изменённые файлы копируются, отсутствующие в source destination-объекты удаляются. Удаление никогда не выполняется до завершения фазы сканирования и построения плана.

Mirror всегда работает с одним VSS snapshot единственного source volume.
Последовательность обязательна:

```text
validate live source identity
→ create VSS snapshot
→ resolve shadow source path
→ scan shadow source and destination
→ load and validate mirror hash catalog
→ delete destination-only objects
→ replace files whose new version is smaller
→ replace changed files of equal size
→ replace growing files and copy new files
→ commit completed mirror generation
→ release VSS snapshot
```

Если VSS snapshot нельзя создать или его identity/path нельзя подтвердить, adapter
завершается с failure до начала копирования и до любых изменений destination.
Fallback на чтение live source запрещён. Единственный source root должен разрешаться
внутри подтверждённого VSS snapshot.

### 32.3.1. Fail-fast policy

Mirror прекращает data operation после первой неожиданной ошибки чтения, записи или изменения destination. После фиксации primary failure выполняются только bounded cleanup: остановка дочерних операций, удаление принадлежащего run временного файла, освобождение VSS и возврат backup-диска offline.

Немедленно останавливают job:

- ошибка чтения обычного source-файла;
- исчезновение source/VSS snapshot во время run;
- ошибка создания, записи, flush или закрытия временного destination-файла;
- недостаток свободного места;
- ошибка атомарной подмены destination-файла;
- несовпадение ожидаемого размера после копирования;
- ошибка удаления destination-only файла или каталога;
- изменение identity source или destination;
- потеря доступа к backup-диску;
- непредусмотренное исключение adapter.

Причины:

- read error может указывать на деградацию source-диска; продолжение увеличивает нагрузку;
- write error может указывать на деградацию backup-диска или системную проблему;
- out-of-space делает дальнейшие copy attempts бессмысленными;
- продолжение после нарушения алгоритмического инварианта затрудняет определение достоверного состояния mirror.

Не останавливают data operation:

- объект исключён явной конфигурацией;
- reparse point пропущен согласно контракту v1;
- не удалось воспроизвести необязательный `mtime` после успешной записи bytes;
- Telegram недоступен;
- не удалось обновить публичный status JSON;
- диагностический log потерял необязательную строку прогресса.

Последние три ошибки обрабатывает manager независимо от результата data operation. Job result и notification/UI health не смешиваются.

Fail-fast не делает весь job транзакционным: destination-only объекты, удалённые после успешного scan, уже отсутствуют; файлы, успешно заменённые до ошибки, остаются новыми; ещё не обработанные — прежними. Run получает `failed`, а следующая операция не начинается после primary failure.

Обязательные свойства:

- source и destination не могут находиться на одном физическом диске;
- root destination проверяется marker-файлом конкретного job;
- запрещено выполнять delete, если source root отсутствует, пуст из-за ошибки доступа или identity source-диска изменилась;
- до destructive phase сохраняется machine-readable plan и его агрегаты;
- отсутствие валидной hash-catalog записи не позволяет признать destination-файл
  неизменённым: такой файл планируется к повторному копированию;
- destination-only объекты удаляются после полностью успешного scan/plan/preflight, но до copy/replace;
- уменьшение mirror выполняется раньше операций, увеличивающих его размер;
- повторный запуск приводит destination к целевому состоянию;
- обязательная архивная нагрузка — bytes обычного файла и его relative path;
- modification time destination сохраняется best effort для удобства, но ошибка его
  установки публикуется только информационным счётчиком/diagnostic и не меняет
  result/health; change detection использует source `mtime` из catalog;
- ACL, owner, audit rules, alternate data streams, hardlinks, sparse layout, compression/encryption flags и прочие NTFS-specific metadata не входят в контракт восстановления v1;
- symbolic links, junctions и другие reparse points не копируются и не
  разыменовываются; adapter публикует только информационный счётчик пропущенных
  объектов, не изменяющий result/health;
- пустые каталоги не считаются самостоятельными архивными данными и могут не воспроизводиться;
- check сверяет destination с сохранённым hash-каталогом: всегда структуру и размеры,
  а в subset/full mode — content hashes; timestamps используются как диагностическая
  метрика и оптимизация, но не как критерий сохранности;
- generation ID идентифицирует успешно завершённое состояние зеркала.

### 32.3.2. Preflight свободного места

Полный metadata scan вычисляет:

- `current_mirror_size` — текущая сумма размеров обычных файлов destination до изменений;
- `planned_mirror_size` — ожидаемая сумма размеров обычных файлов после успешного run;
- `largest_copy_size` — максимальный размер одного нового или изменённого файла, который будет создан как sibling temp.

Пиковая оценка mirror:

```text
required_peak_mirror_size =
    max(current_mirror_size, planned_mirror_size)
    + largest_copy_size
```

Формула является верхней границей только при обязательном порядке:

1. удалить destination-only;
2. выполнить уменьшающие замены;
3. выполнить замены равного размера;
4. выполнить увеличивающие замены и новые файлы.

Preflight сравнивает требуемый peak с ёмкостью тома с учётом места, занятого вне данного mirror root. Если hard requirement не выполняется, job завершается `out_of_space` до первой destructive/write operation.

Hard capacity и capacity-growth warning различаются:

- если `required_peak_mirror_size` физически не помещается с учётом прочих данных тома, job получает `failed` до любых изменений;
- фиксированный или процентный минимальный резерв не задаётся;
- `positive_growth_bytes = max(0, size_after - size_before)`;
- после backup вычисляется `remaining_free_bytes` на destination volume;
- если `positive_growth_bytes > remaining_free_bytes × 0.10`, создаётся storage health `warning` и немедленное Telegram-уведомление;
- для preflight используется та же формула с прогнозными `planned_mirror_size` и free-after;
- job при этом всё равно выполняется, если hard capacity достаточна;
- успешный data result остаётся `success`: быстрый рост учитывается отдельно как состояние capacity;
- warning дедуплицируется по конкретному run и повторяется при каждом новом
  нарушающем росте следующего run.

Для `snapshot` положительный рост определяется по физическому изменению размера repository между началом и концом operation. Для `mirror` — по изменению итогового размера mirror root. Временный sibling-файл не считается постоянным ростом.

Даже успешный preflight не отменяет runtime обработку `disk full`: внешние процессы, NTFS overhead, изменение доступного места и системные резервы могут изменить результат. Любой runtime out-of-space немедленно останавливает job.

Mirror является оперативной репликой и не заявляется как защита от удаления или шифрования source.

### 32.3.3. Атомарная подмена файла

Каждый новый или изменённый файл обновляется отдельно:

```text
copy VSS source bytes
→ sibling temp file in destination directory
→ flush file data
→ verify resulting length
→ close all handles
→ calculate content hash of the completed temp file
→ atomically replace and flush the catalog entry with the new metadata/hash
→ replace old final with temp on the same volume
```

Временное имя содержит run ID и непредсказуемый suffix, не конфликтует с пользовательскими именами и распознаётся recovery-cleanup. Temp создаётся на том же destination-томе, чтобы финальная подмена не превратилась в copy между томами.

До успешной атомарной подмены прежний destination-файл остаётся доступным и не изменяется. Отдельный шаг `delete old → rename new` запрещён, поскольку создаёт окно без восстановимой версии. Реализация использует Windows replace/move primitive с replace-existing и write-through semantics. При сбое подмены старый final считается сохранным, temp удаляется в cleanup, а job немедленно завершается failure.

Для нового destination-файла применяется тот же алгоритм, но final ещё отсутствует. Job-level atomicity не заявляется.

Hash вычисляется по временному destination-файлу после завершения и flush копии, то
есть описывает именно bytes, которые будут опубликованы как final. Повторное чтение
обычно обслуживается файловым cache Windows, но корректность не зависит от наличия
cache. Только после вычисления hash разрешена атомарная подмена.

NTFS rename и commit каталога являются двумя разными durable-операциями и не могут
быть общей атомарной транзакцией. Catalog поэтому хранит не историю old/new, а одно
текущее желаемое состояние файла. Атомарная и flush-нутая смена catalog entry со
старого hash на новый является commit point: до неё temp не считается принятым,
после неё recovery обязан докатить temp в final.

Temp identity детерминированно связывается с relative path и run, но отдельная
pending-запись с двумя hashes не создаётся. Recovery читает текущий единственный hash
из catalog, хэширует фактически существующие final/temp и применяет таблицу:

| Фактическое состояние | Решение |
|---|---|
| final соответствует catalog hash | Желаемое состояние уже опубликовано; удалить принадлежащий run temp, если он остался |
| final не соответствует catalog hash, temp соответствует | Catalog уже переключён, публикация не завершена: удалить/заменить старый final и переименовать temp в final |
| final отсутствует, temp соответствует catalog hash | Старый final уже удалён: переименовать temp в final |
| final соответствует catalog hash, temp не соответствует | Catalog ещё описывает старое состояние: temp не принят и удаляется |
| ни final, ни temp не соответствует catalog hash | Неизвестное/повреждённое состояние: `failed/critical`, ничего автоматически не перезаписывать |
| final не соответствует catalog hash, temp отсутствует | Желаемые bytes потеряны: `failed/critical` |

Сравнение выполняется по content hash и ожидаемому размеру, а не только по имени,
mtime или наличию файла. Если чтение для классификации невозможно, результат также
`failed/critical`.

Catalog update сам выполняется через `temp → flush → atomic replace`; после сбоя
валидна либо старая, либо новая запись. Повреждённая/нечитаемая запись не угадывается
и даёт `failed/critical`. После recovery выполняется обычный scan/plan. Расхождение
между единственным желаемым hash и файлами распознаётся, докатывается только в
однозначных случаях и никогда не принимается молча.

Удаление destination-only файла использует ту же модель желаемого состояния. До
`DeleteFileW` catalog entry атомарно заменяется и flush-ится tombstone-значением
`absent`. После подтверждённого отсутствия final завершённый tombstone удаляется.
Recovery применяет правила:

| Catalog и filesystem | Решение |
|---|---|
| `absent`, final существует | Удаление принято: повторить delete |
| `absent`, final отсутствует | Удаление завершено: убрать tombstone |
| hash, final соответствует hash | Удаление не было принято: сохранить файл |
| hash, final отсутствует | Неожиданная потеря: `failed/critical` |
| hash, final не соответствует hash | Повреждение/внешнее изменение: `failed/critical` |

Tombstones создаются только после полностью успешных scan, plan и preflight.
Каталоги удаляются после обработки содержащихся файлов; пустые каталоги не являются
самостоятельными архивными данными и отдельного content tombstone не требуют.

### 32.3.4. Mirror hash catalog

Каждый mirror root содержит служебный каталог `.backup-system`, исключённый из
пользовательского namespace и destructive mirror-plan. В нём атомарно хранятся:

- schema и hash algorithm version;
- job marker и generation ID;
- для каждого обычного файла: relative path, size, source `mtime_ns` и content hash;
- детерминированная связь зарезервированных temp-файлов с catalog entries;
- scrub cursor и дата последнего полного scrub-cycle.

Физический формат catalog v1 — отдельная SQLite database
`.backup-system\catalog.sqlite3` на том же destination volume. Она принадлежит
mirror adapter и не связана с manager SQLite. Обязательные настройки:

```text
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
```

Минимальная логическая schema:

```sql
CREATE TABLE catalog_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE mirror_entries (
    path_key TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL,
    desired_state TEXT NOT NULL CHECK (desired_state IN ('present', 'absent')),
    size_bytes INTEGER,
    source_mtime_ns INTEGER,
    sha256 BLOB,
    content_generation TEXT,
    verified_at TEXT,
    temp_relative_path TEXT,
    generation_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
      (desired_state = 'present' AND size_bytes IS NOT NULL AND sha256 IS NOT NULL)
      OR desired_state = 'absent'
    )
);
```

`relative_path` хранит исходное написание пути для восстановления. `path_key` —
канонический регистронезависимый ключ для уникальности и lookup согласно Windows
ordinal case-insensitive semantics; Python `str.lower()`/`casefold()` не считается
достаточной заменой Win32-сопоставлению. Разделители и компоненты пути
канонизируются до записи, абсолютные пути и `..` запрещены.

Если source содержит два объекта, которые destination/Win32 считает одним путём
(например, они различаются только регистром в case-sensitive каталоге), scan
завершается `failed` до plan/preflight и любых изменений destination. Catalog всегда
сохраняет не более одной записи на `path_key`, но публикует пользователю исходный
`relative_path`.

Переключение entry со старого hash на новый hash/temp identity или на `absent`
выполняется одной SQLite transaction. После публикации final отдельная идемпотентная
transaction очищает `temp_relative_path`; незавершённость этого шага безопасно
распознаётся recovery. Перед переводом backup-диска offline executor выполняет WAL
checkpoint, закрывает все database handles и flush-ит связанные filesystem handles.
Ошибка transaction, checkpoint или закрытия catalog немедленно завершает mirror run
как `failed/critical`.

При открытии выполняются schema/version validation и SQLite integrity check
подходящего для startup режима. Повреждённый catalog автоматически не чинится и не
перестраивается из непроверенного destination; применяется ранее определённая
процедура полного перестроения через повторное копирование из VSS source.

Hash algorithm v1 — `SHA-256`. Все content hashes в одном mirror catalog обязаны
иметь этот algorithm/version; смешивание алгоритмов запрещено. Используется
стандартная реализация Python/OpenSSL без дополнительной криптографической
зависимости. Изменение алгоритма в будущей версии требует явной миграции либо полного
перестроения catalog и не выполняется неявно во время обычного backup.

Для mirror не используются статические subset buckets. Scheduled check применяет
age-based scrub с целевым бюджетом `25%` текущего логического размера mirror:

1. завершить journal recovery и убедиться, что нет неразрешённых desired states;
2. полностью просканировать metadata destination, исключая `.backup-system` и
   распознанные adapter temp-файлы;
3. сверить набор `path_key`, типы объектов и размеры со всеми `present` catalog
   entries;
4. при missing, unexpected, type mismatch или size mismatch немедленно завершить
   check как `failed/critical`, не обращаясь к текущему source;
5. выбрать `present` entries по возрастанию `verified_at`, отсутствующее значение —
   первым;
6. читать каждый выбранный final целиком и сравнивать SHA-256 с catalog;
7. продолжать, пока сумма размеров не достигнет 25% logical bytes;
8. файл, пересекающий границу бюджета, всегда дочитывать целиком;
9. после успешного сравнения атомарно обновить `verified_at` текущей
   `content_generation`.

Полный metadata scan является обязательной и неотключаемой частью каждого mirror
check. Он не создаёт VSS snapshot и не читает source: эталоном состояния mirror
служит durable catalog. `mtime` сверяется только как диагностическое поле; совпадение
или различие времени не заменяет проверку size/hash и самостоятельно не определяет
целостность bytes.

Новый/изменённый файл получает новую `content_generation` и `verified_at` в момент,
когда hash полностью записанного temp принят в catalog: эти bytes только что были
прочитаны с destination и считаются проверенными. Неизменённые файлы сохраняют
прежний `verified_at`. Поэтому check преимущественно читает самые давно не
проверявшиеся bytes, а не недавно скопированные изменения.

При стабильном mirror четыре check-запуска покрывают примерно весь объём за 20
недель при цикле `4 backup → check`. UI публикует объём последнего scrub, долю bytes
с `verified_at` не старше 20 недель и возраст самой старой непроверенной entry.
Статус «полный круг актуален» означает, что все текущие `present` entries были
успешно проверены или созданы в пределах целевого 20-недельного периода; это не
привязано к искусственным номерам четвертей.

Если один файл превышает 25% mirror, он всё равно проверяется целиком. Chunk hashes
и частичная проверка одного файла не входят в v1.

Catalog является проверочным индексом, а не единственным носителем backup-данных.
Его потеря не удаляет mirror-файлы, но делает их целостность неподтверждённой:
автоматически доверять существующим файлам или строить новые hashes только по
непроверенному mirror запрещено. Следующий backup должен заново скопировать такие
файлы из VSS source и построить hashes по temp-копиям либо завершиться failure, если
source уже недоступен.

### 32.3.5. Copy engine

В v1 внешний copy engine не используется. Mirror adapter вызывает Windows API через типизированную обёртку Python:

- `CopyFile2` копирует bytes из VSS source во временный sibling-файл;
- `FlushFileBuffers` вызывается до закрытия временного файла;
- длина временного файла повторно читается и сравнивается с source;
- `ReplaceFileW` атомарно заменяет существующий final;
- `MoveFileExW` с replace/write-through semantics устанавливает новый final там, где прежнего файла нет;
- `DeleteFileW`/`RemoveDirectoryW` используются только для destination-only объектов из полностью построенного и провалидированного plan;
- Win32 error code немедленно преобразуется в типизированную ошибку adapter без разбора локализованного текста.

Все handle-объекты закрываются детерминированно. Cancel flag передаётся в `CopyFile2` там, где позволяет API; после cancel запрещено выполнять replace. Python `shutil.copy*`, shell-команды и `robocopy` не входят в production data path.

Обёртка Windows API покрывается contract tests с fake boundary и отдельными hardware integration tests. Имена функций, flags, структуры и правила владения памятью централизованы в одном модуле; вызовы Win32 не размазываются по бизнес-логике adapter.

### 32.3.6. Контракт сохранности основных данных

Восстановление считается полным, если для каждого обычного source-файла, не исключённого конфигурацией:

- существует файл с тем же relative path;
- длина содержимого совпадает;
- полная hash-проверка подтверждает идентичность bytes.

Имена каталогов сохраняются постольку, поскольку они являются частью relative path
файла. Потеря ACL или специального NTFS-атрибута не переводит backup в
warning/failed. Пропущенные reparse points отображаются отдельным информационным
счётчиком и не считаются непрочитанными обычными файлами.

### 32.3.7. Обнаружение изменений

Обычный `mirror backup` не хэширует содержимое всех source-файлов. Source-файл
считается неизменённым только при выполнении всех условий:

```text
unchanged := catalog entry exists and is valid
             AND catalog.desired_state == present
             AND no unresolved temp/tombstone exists
             AND source.path_key == catalog.path_key
             AND source.size == catalog.size_bytes
             AND source.mtime_ns == catalog.source_mtime_ns
             AND destination is a regular file
             AND destination.size == catalog.size_bytes
```

Если всё условие истинно, содержимое source/destination не читается и сохранённый
SHA-256 остаётся действующим. Любое несовпадение или неполная catalog-запись выбирает
безопасный путь: файл копируется из VSS source во временное имя, flush-ится,
хэшируется и публикуется через journal protocol. Source `mtime` устанавливается на
final best effort, но fast path сравнивает source metadata с сохранённой source
metadata каталога, а не полагается на успешное воспроизведение destination mtime.

Следствия:

- изменение bytes с намеренным сохранением прежних size и mtime не обнаруживается обычным backup;
- защита от такого случая обеспечивается age-based scheduled check и ручным full
  check destination против hash-каталога;
- каждый check сначала сравнивает полное дерево и размеры без чтения всего
  содержимого;
- scheduled check затем сравнивает hashes самых давно проверенных 25% logical bytes;
- manual full check сравнивает content hashes всех обычных файлов;
- hash mismatch всегда имеет severity `critical`, даже если size и mtime совпадают.

Алгоритм не использует Windows archive bit: несколько независимых jobs могут читать один source и не должны менять его состояние.

Metadata scan выполняется нативным Win32 directory enumeration
(`FindFirstFileExW`/`FindNextFileW` либо строго эквивалентной API-обёрткой). Из одной
directory entry читаются имя, тип/attributes, размер и `LastWriteTime`; отдельный
handle для запроса каждого из этих полей не открывается. Время хранится без потери
точности как исходное UTC `FILETIME` (100 ns units), без промежуточного преобразования
через локальное время или секунды.

Creation time не участвует в change detection: оно не доказывает неизменность bytes
и может меняться при копировании/восстановлении. Отдельный file handle и чтение
содержимого требуются только для файла, выбранного для copy или hash verification.

## 33. Exit codes executor

| Code | Класс |
|---:|---|
| 0 | success |
| 10 | success_with_warning |
| 20 | config_invalid |
| 21 | disk_not_found |
| 22 | disk_identity_mismatch |
| 23 | repository_unavailable |
| 24 | source_read_error/incomplete_backup |
| 25 | backup_engine_failed |
| 26 | verification_failed |
| 27 | restore_test_failed |
| 28 | disk_offline_failed |
| 29 | cancelled |
| 30 | internal_error |

Exit code не задаёт retry policy: автоматических retry нет. Health severity и
необходимость startup recovery определяются типизированным terminal event и
состоянием disk lifecycle, а не третьей колонкой таблицы.

Terminal JSON event является основным результатом, exit code — обязательной проверкой согласованности. Если они противоречат друг другу, manager классифицирует run как `internal_error`.

## 34. SQLite schema v1

Минимальные таблицы:

```sql
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    config_valid INTEGER NOT NULL,
    config_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE schedule_state (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    slot_counter INTEGER NOT NULL,
    recovery_check_required INTEGER NOT NULL DEFAULT 0,
    last_evaluated_at TEXT NOT NULL,
    next_fire_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE safety_latches (
    latch_key TEXT PRIMARY KEY,
    latch_type TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    source_run_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    cleared_at TEXT
);

Per-job verification gate не требует отдельной таблицы: он выводится из
`schedule_state.recovery_check_required` либо текущей check-фазы и terminal result её
последней operation. Manager проверяет gate транзакционно перед принятием manual или
scheduled backup.

CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    deduplication_key TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    kind TEXT NOT NULL,
    mode TEXT,
    trigger_source TEXT NOT NULL,
    scheduled_at TEXT,
    queued_at TEXT NOT NULL,
    state TEXT NOT NULL,
    removed_at TEXT,
    terminal_reason TEXT
);

CREATE UNIQUE INDEX uq_operations_unfinished_job_kind
ON operations(job_id, kind)
WHERE state IN ('queued', 'running');

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    result TEXT,
    stage TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    snapshot_id TEXT,
    stage_started_at TEXT,
    progress_updated_at TEXT,
    files_done INTEGER,
    files_total INTEGER,
    bytes_done INTEGER,
    bytes_total INTEGER,
    bytes_added INTEGER,
    warning_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    disk_offline_confirmed INTEGER NOT NULL DEFAULT 0,
    diagnostics_log_date_from TEXT,
    diagnostics_log_date_to TEXT
);

CREATE TABLE run_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    emitted_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE notifications (
    notification_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(run_id),
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE physical_disks (
    disk_id TEXT PRIMARY KEY,
    public_disk_id TEXT NOT NULL UNIQUE,
    model TEXT,
    media_type TEXT,
    bus_type TEXT,
    capacity_bytes INTEGER,
    role TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE disk_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    disk_id TEXT NOT NULL REFERENCES physical_disks(disk_id),
    observed_at TEXT NOT NULL,
    operational_state TEXT NOT NULL,
    smart_health TEXT NOT NULL,
    temperature_celsius INTEGER,
    power_on_hours INTEGER,
    reallocated_sectors INTEGER,
    pending_sectors INTEGER,
    offline_uncorrectable INTEGER,
    interface_crc_errors INTEGER,
    nvme_percentage_used INTEGER,
    nvme_media_errors INTEGER,
    normalized_json TEXT NOT NULL
);

CREATE TABLE volumes (
    volume_id TEXT PRIMARY KEY,
    public_volume_id TEXT NOT NULL UNIQUE,
    disk_id TEXT NOT NULL REFERENCES physical_disks(disk_id),
    display_name TEXT,
    label TEXT,
    filesystem TEXT,
    role TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE volume_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id TEXT NOT NULL REFERENCES volumes(volume_id),
    observed_at TEXT NOT NULL,
    online INTEGER NOT NULL,
    total_bytes INTEGER,
    free_bytes INTEGER
);

CREATE TABLE backup_metrics (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    source_logical_bytes INTEGER,
    protected_logical_bytes INTEGER,
    retained_logical_bytes INTEGER,
    bytes_read INTEGER,
    bytes_written INTEGER,
    repository_added_bytes INTEGER,
    repository_physical_bytes INTEGER,
    repository_free_bytes INTEGER,
    observed_at TEXT NOT NULL
);
```

Поле `operations.state` принимает `queued|running|completed|removed`. `completed`
означает наличие терминального run независимо от его результата; `removed` означает
удаление из очереди до создания run. Поле `runs.state` принимает `running|finished`,
а фактический исход `success|warning|failed|cancelled|interrupted` хранится отдельно
в `runs.result`. Переход operation `queued → running` и создание её единственного run
выполняются одной транзакцией; завершение run и переход operation
`running → completed` также выполняются одной транзакцией.

`operation` создаётся при принятии работы в очередь. `run` создаётся только при
переходе operation в `running` и фактическом запуске executor; у operation со
статусом `removed` run отсутствует. Terminal metadata operations и runs хранится
бессрочно: при ожидаемом числе backup-операций это компактная аудит-история.

Progress events не добавляются в `run_events`: поля текущего progress обновляются на
месте в одной строке активного `runs`. Таблица `run_events` содержит только редкие
значимые события и переходы стадий, необходимые для расследования. Потеря очередного
необязательного progress update не влияет на итог run. Периодические UI projection,
свободное место и прочие часто обновляемые значения также заменяют текущее состояние,
а не образуют временной ряд. Детальные stdout/stderr входят в дневные внешние логи.
В локальные `00:00` открывается новый файл, после чего удаляются файлы старше 60
суток. Size limit и досрочное удаление в v1 отсутствуют. Удаление лог-файла не удаляет
operation, run, terminal result, агрегированные метрики или структурированные
warning/error из SQLite.

Если manager SQLite или строка `schedule_state` конкретной job потеряна/повреждена,
manager не угадывает прежний `slot_counter` и не начинает четыре backup вслепую. Он
создаёт новое состояние с `slot_counter = 0` и
`recovery_check_required = 1`, публикует `unknown/critical` и включает проблему в
startup Telegram report. Немедленного catch-up нет: на следующем штатном cron-слоте
вместо `cycle[0]` выполняется check. Только terminal success/warning проверки снимает
recovery flag; следующей scheduled operation становится первый backup нового цикла.
Failed проверка оставляет recovery flag и повторяется на следующем cron.

Recovery-check при потере manager schedule state всегда имеет полный scope,
независимо от обычной дробной политики job: snapshot/restic выполняет
`check --read-data`, mirror выполняет полный metadata scan и SHA-256 всех `present`
entries. Успешный full check устанавливает новую точку полного покрытия: restic
subset cursor начинает новый круг с `1/4`, а все текущие mirror entries получают
актуальный `verified_at`. Metadata-only или subset check recovery flag не снимает.

Все timestamps хранятся как RFC3339 UTC с `Z`; локальная timezone применяется только при расчёте расписания и отображении. SQLite работает в WAL mode. Каждая смена terminal state выполняется одной транзакцией.

## 35. Command spool contract

Пример ручной команды:

```json
{
  "schema_version": 1,
  "command_id": "<uuid4>",
  "created_at": "<rfc3339-utc>",
  "kind": "run",
  "job_id": "data",
  "operation": "backup"
}
```

Допустимые `kind` v1:

- `run` с job operation
  `backup|check|prune|restore|restore-test|repair-mirror|recover`;
- `cancel-current` без `job_id` и operation;

`backupctl` не принимает произвольную executable, restic arguments, absolute source
path, repository path или mount point. Restore-команда является единственным
исключением для пользовательских путей: она принимает один относительный source
selection и один абсолютный локальный parent target по правилам раздела 18. Имя
входного файла равно `<command_id>.json`. Manager проверяет совпадение имени и
payload, размер файла, schema version, UUID, возраст команды и allowlist полей;
executor повторно проверяет filesystem identities перед записью.

## 36. Публичные JSON-контракты

Все публичные файлы содержат `schema_version`, `generation_id` и `generated_at`. UI не объединяет файлы разных `generation_id`: при несовпадении повторяет запрос.

`health.json`:

```json
{
  "schema_version": 1,
  "generation_id": "<uuid>",
  "generated_at": "<rfc3339>",
  "manager_state": "idle",
  "manager_started_at": "<rfc3339>",
  "version": "0.1.0"
}
```

`status.json` содержит:

- агрегаты jobs и их backup metrics;
- активный run и ближайшие операции; для активного run публикуются operation kind,
  job display name, stage, run/stage elapsed, состояние executor process и возраст
  последнего progress event,
  а для измеримой стадии — files/bytes done и total, скорость и достоверная ETA;
- ровно два последних run каждой job: последний и непосредственно предыдущий, а
  также её следующее cron-срабатывание и operation следующей фазы цикла;
- физические диски с нормализованным SMART;
- для ключевых SMART-показателей — текущее значение, предыдущее сопоставимое
  значение, направление/величина изменения, время последнего регресса и изменения
  за 24 часа и 30 дней;
- тома и свободное место;
- публичное состояние backup-диска (`offline|online_during_backup|error|unknown`) без serial/GUID/path;
- список текущих системных health-проблем (SMART, capacity, freshness, disk state),
  не являющийся отдельной очередью подтверждения ошибок runs.

Dashboard показывает активную operation отдельной верхней карточкой и обновляет её
обычным polling вместе с остальным `status.json`. Неизмеримая стадия отображается как
активная работа с elapsed и состоянием executor process, без фиктивной progress bar.
Возраст последнего progress event показывается как диагностическая информация, но
сам по себе не окрашивает operation как зависшую. Очередь публично
показывает только безопасные display names, operation kinds, позиции и время
ожидания, без локальных путей и внутренних идентификаторов устройств.
`operation_id` не считается секретом и публикуется для active и queued operations.
В основной строке UI он не создаёт визуальный шум: полный UUID находится в
раскрываемых деталях и снабжён локальной кнопкой копирования. Это позволяет точно
передать ID в `backupctl queue remove` без управления через Web.

Dashboard всегда содержит единый блок текущего исполнения. Если active operation и
queued operations отсутствуют, блок состоит из одной явной строки: «Сейчас ничего не
выполняется». Если executor работает, его operation является первой строкой блока со
статусом `running`, даже когда ожидающей очереди нет; в этой строке показываются stage
и progress. Следом отображаются все `queued` operations в фактическом порядке
исполнения: позиция, job display name, operation kind, источник trigger
(`scheduled|manual`) и elapsed waiting. Таким образом термин «очередь» в UI включает
и текущую running operation, и ожидающий хвост, а штатный пустой экран остаётся
однозначным.

Само наличие одной или нескольких `queued` operations не меняет общий health и не
создаёт warning: ручная операция может штатно ожидать текущую работу. Health и alerts
меняются только по самостоятельной содержательной причине, включая scheduled
overlap, превышение configured deadline, safety latch либо ошибку operation.

Произвольная публичная история runs и исторические графики в v1 отсутствуют. Полная
история сохраняется локально в SQLite и доступна администратору, но не публикуется
через nginx.

При публикации manager формирует оба файла с одним generation ID, атомарно заменяет
`status.json`, а `health.json` заменяет последним. UI использует `health.json` как
commit marker поколения.

## 37. Зависимости и сборка

Предварительный минимальный набор:

- Python `>=3.12,<3.15`;
- Pydantic v2 — строгие схемы;
- PyYAML — YAML parsing через `safe_load`;
- croniter — расчёт cron;
- comtypes — явно описанные low-level Windows VSS COM interfaces;
- pywin32 — DPAPI, Job Objects и Windows integration;
- httpx — Telegram HTTPS;
- pytest, pytest-timeout — тесты;
- Ruff — lint/format;
- mypy — статическая проверка типов.

Версии фиксируются lock-файлом. Production deployment состоит из обычных `.py`
sources и отдельного private `.venv`; PyInstaller, Nuitka и иной Python-to-EXE bundle
не используются. Установка выполняет frozen sync lock-файла заранее и не скачивает
зависимости при запуске service или job. NSSM запускает абсолютный
`<venv>\Scripts\python.exe -m backup_system.manager`. Manager не принимает
настраиваемый executable path, а запускает executor тем же `sys.executable` через
`-m backup_system.executor`, без shell.

Packaging metadata объявляет console entry point `backupctl`; стандартная установка
Python-пакета создаёт обычный launcher `<venv>\Scripts\backupctl.exe`. Это только
генерируемый entry-point shim в Python CLI, а не упакованная копия приложения.
Закреплённые нативные `restic.exe` и `smartctl.exe` остаются в `bin`, а deployment
manifest содержит их версии и SHA-256.

### 37.1. Dev-to-Stable deployment v1

Пользовательский updater, online update и автоматическое получение релизов в v1 не
реализуются. На машине существуют два разных дерева:

- Git-controlled Dev directory, где редактируется и тестируется проект;
- отдельный Stable directory, из которого NSSM запускает production service.

Перенос выполняет отдельный Python deploy script из Dev. Скрипт проверяет, что уже
запущен с administrative token, а при обычном запуске перезапускает самого себя через
Windows UAC `runas` и ждёт elevated child result. PowerShell deployment logic и shell
command construction не используются; NSSM и остальные процессы вызываются через
`subprocess` со списком аргументов.

Elevated deploy выполняет последовательность:

1. Проверяет source/target identities, Git revision и deployment manifest.
2. Штатно останавливает NSSM service и ждёт полного cooperative cleanup; если service
   не остановился, deploy завершается и Stable не изменяет.
3. Копирует в staging только явно перечисленный release-состав Dev, не перенося
   `.git`, Dev `.venv`, caches, test artifacts и локальные временные файлы.
4. Создаёт для подготавливаемой Stable-версии собственную `.venv` и выполняет
   `uv sync --frozen` строго по её lock-файлу. Dev `.venv` никогда не копируется и
   production service её не использует. Затем запускает config/schema validation и
   smoke checks до переключения service на новый код.
5. Заменяет Stable application files подготовленной версией, запускает NSSM service
   и проверяет его startup health.

Deploy является наблюдаемой частью developer workflow, а не unattended updater.
Автоматический или встроенный rollback отсутствует; предыдущая release-копия ради
rollback не создаётся. Если ошибка произошла после замены Stable, новая версия
остаётся на месте, service остаётся в фактическом состоянии `stopped`/`failed`, а
скрипт возвращает non-zero exit code и печатает этап и диагностику. Разработчик
исследует и исправляет проблему, затем повторяет deploy. Попытка автоматически
скрыть ошибку запуском старого кода запрещена.

Operational config, SQLite, spool, исходные logs и публичные projections находятся
только в Stable `data` и deploy-скриптом никогда не копируются из Dev и не
заменяются. Скрипт не считается общим installer/updater и обслуживает только этот
локальный Dev→Stable workflow.

Dirty Git working tree разрешён: deploy переносит фактическое текущее содержимое
release-файлов и не требует commit. Конкретный формат manifest, точный copy algorithm
и UX developer script определяются по месту при его реализации и не входят в
product v1 contract. Дальнейшее проектирование deploy/updater framework в этом
документе не требуется.

Нельзя добавлять web framework: status UI статический, HTTP backend отсутствует.

## 38. Тестовая стратегия

### 38.1. Unit

- строгая валидация обеих схем конфигурации;
- cron, timezone, operation cycle и отсутствие catch-up для пропущенных
  срабатываний;
- отсутствие продвижения slot counter для любого пропущенного срабатывания;
- продвижение только после terminal success/warning плановой operation; ручные
  backup/restore-test счётчик не меняют, manual check может разблокировать check;
- coalescing повторных triggers при уже queued/running operation;
- state machine manager;
- классификация exit code/event;
- redaction публичной проекции;
- spool validation и deduplication;
- Telegram formatting без сетевого вызова;
- restic JSON parser на fixtures.

### 38.2. Contract

- каждый event соответствует versioned JSON schema;
- executor stdout не содержит не-JSON строк;
- public JSON не содержит запрещённых полей и известных секретов;
- manager package не импортирует executor package;
- executor package не импортирует manager/database/telegram;
- nginx-каталог не используется как вход manager.

### 38.3. Integration без реального диска

- fake restic process: success, warning, malformed JSON, hang, crash;
- fake disk adapter: identity mismatch, online timeout, offline failure;
- manager restart между каждой сменой состояния;
- SQLite migration и recovery;
- повторная доставка одной spool-команды;
- отказ Telegram и последующая retry;
- атомарность status projection при принудительном завершении.

### 38.4. Windows hardware tests

Выполняются только на выделенном тестовом диске с подтверждённым serial:

- обязательный VSS binding PoC до backup adapters: создать client-accessible snapshot
  тестового NTFS volume, получить и проверить shadow device path, прочитать известный
  файл через snapshot, удалить exact SnapshotSetID и подтвердить отсутствие orphan;

- offline/online и автоматическое восстановление mount point;
- повторное назначение mount point при необходимости;
- неверный marker;
- отключение кабеля;
- reboot во время restic;
- принудительное завершение executor;
- VSS с открытым файлом;
- Unicode, кириллица, emoji и длинные пути;
- restic backup/check/restore;
- заполнение репозитория;
- проверка отсутствия записи на неподтверждённый диск.

Destructive hardware tests требуют отдельного флага `BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID`; без него тесты должны завершаться как skipped.

### 38.5. Приёмочный сценарий

1. Диск offline.
2. Scheduler создаёт operation.
3. Executor подтверждает identity и включает диск.
4. Создаёт restic snapshot тестового блока через VSS.
5. Возвращает диск offline.
6. Manager фиксирует success и публикует JSON; результат входит в ближайший
   суточный Telegram heartbeat-отчёт.
7. Следующий запуск добавляет только изменения.
8. Удалённый из source файл остаётся в предыдущем snapshot.
9. Примерно через месяц после запуска manual restore-test восстанавливает контрольные
   данные и сверяет SHA-256; сценарий также доступен в приёмочных тестах до production.
10. После принудительного сбоя система возвращает или явно помечает диск как требующий recovery.

## 39. Требования к Status UI v1

### 39.1. Назначение

Status UI должен без перехода к локальным логам отвечать на вопросы:

1. Все ли физические диски исправны?
2. Сколько места занято и свободно на каждом томе и backup-репозитории?
3. Какие данные защищены и насколько свежа последняя успешная копия?
4. Уложился ли последний запуск каждой job в заданный для неё deadline?
5. Чем завершились последний и предыдущий runs каждой job?

UI не содержит кнопок, форм, POST-запросов, ссылок-команд или скрытых управляющих endpoint-ов.

### 39.2. Главный экран

Порядок блоков сверху вниз:

1. Общий health banner.
2. Карточки backup jobs.
3. Физические диски и их SMART.
4. Тома и свободное место.

Верхний banner показывает наихудшее из независимых состояний:

- свежесть manager heartbeat;
- состояние backup jobs;
- SMART физических дисков;
- свободное место;
- соблюдение deadline отдельными jobs;
- состояние offline backup-диска;
- свежесть проверки репозитория.

Цвет никогда не является единственным носителем информации: рядом всегда присутствуют текстовый статус и причина.

### 39.3. Карточка backup job

Обязательные поля:

- display name и job kind (`snapshot`, `mirror`, `maintenance`);
- health и текстовая причина;
- время, статус и длительность последнего запуска;
- время, статус и длительность непосредственно предыдущего запуска;
- время последнего успешного запуска;
- возраст защищённой копии;
- следующее cron-срабатывание, operation следующей фазы цикла и настроенный deadline;
- logical source size;
- logical size последнего snapshot/копии;
- bytes read и bytes written последним запуском;
- bytes added to repository последним запуском;
- число обработанных, новых, изменённых, удалённых и ошибочных файлов, если поддерживает adapter;
- snapshot ID или mirror generation ID;
- время последней metadata/sample/full проверки;
- состояние retention;
- текущая стадия, если job выполняется.
- непрерывно обновляемая фактическая длительность текущего run; при наложении jobs
  рядом показывается явный диагностический признак возможного зависания.

`last_run` — run с самым поздним временем создания, включая `queued` и `running`.
Если job сейчас выполняется, текущий run занимает слот последнего, а
`previous_run` содержит непосредственно предшествующий ему run. UI не добавляет
третий слот с ещё одним завершённым запуском.

Ошибка принадлежит только конкретному run и не создаёт отдельный объект alarm,
который требуется подтверждать. Пока failed run является последним, карточка job
показывает его ошибочный статус. После успешного запуска он перемещается в
`previous_run`, затем исчезает из публичной проекции, но навсегда остаётся в
локальной истории SQLite. Ручного acknowledge/resolve в v1 нет.

Timeline, недельный календарь, прогноз окончания очереди и статистические оценки
длительности в UI v1 отсутствуют. Нарушение deadline показывается как отдельный
признак конкретного run и не изменяет его backup-result.

### 39.4. Размеры backup

UI обязан различать:

- `source logical size` — сумма логических размеров выбранных исходных файлов;
- `latest protected logical size` — объём данных, видимый при восстановлении последней версии;
- `retained logical size` — сумма логических размеров удерживаемых версий; может содержать повторения;
- `last run added bytes` — новые физические данные, добавленные последним запуском;
- `repository physical size` — реально занято файлами репозитория;
- `repository volume free/total` — свободное и полное место файловой системы.

Поскольку каждая snapshot job имеет отдельный restic repository, его фактический
physical size является точным physical size этой job. Дедупликация действует между
snapshots внутри job, но не между разными jobs. UI не маркирует это значение как
estimate.

Размеры отображаются в IEC (`GiB`, `TiB`), а API хранит целые bytes.

### 39.6. Раскладка физических дисков

Каждый физический диск отображается отдельной карточкой независимо от числа разделов:

- стабильный публичный `disk_id`, не равный serial;
- model/family;
- тип: HDD/SSD/NVMe/unknown;
- интерфейс/bus;
- полная ёмкость;
- operational state: online/offline/missing/unknown;
- роль: system/source/backup/surveillance/torrents/unclassified;
- температура и время измерения;
- SMART overall health;
- power-on hours;
- power cycle count;
- reallocated sectors;
- current pending sectors;
- offline uncorrectable sectors;
- reported uncorrectable errors;
- interface CRC errors;
- NVMe critical warning, percentage used и media/data integrity errors;
- результат и время последнего self-test;
- предыдущее сопоставимое значение и последнее изменение ключевых показателей;
- изменение ключевых counters за 24 часа и 30 дней и время последнего регресса;
- список размещённых на диске томов.

Неподдерживаемое конкретным устройством поле отображается как `not reported`, а не `0`.

### 39.7. Тома и свободное место

Для каждого тома:

- публичный `volume_id`;
- drive letter или display mount name;
- label;
- filesystem;
- physical disk relation;
- total, used и free bytes;
- free percent;
- роль;
- время измерения.

Volume monitoring является allowlist-driven. Manager опрашивает через
`GetDiskFreeSpaceExW` только явно перечисленные `monitoring.volumes.items`,
идентифицированные устойчивым volume GUID, а не текущей буквой. Неуказанные внешние,
removable и вновь появившиеся volumes полностью игнорируются и не публикуются.

Настроенный volume может отсутствовать, быть размонтирован или находиться на штатно
offline диске. Это не monitoring error и не alert: UI показывает последнее известное
значение и явный признак `stale`, а при отсутствии истории — `unknown`, но никогда не
подставляет нулевое свободное место. Недоступность становится ошибкой только если
конкретная executor job фактически требует этот source/destination и не может
выполнить свой lifecycle.

Manager хранит и публикует только конфигурационный display ID/name; volume GUID и
текущие mount paths в LAN JSON не попадают. Poll interval задаётся конфигом, типичное
значение — 60 секунд. Один зависший query имеет bounded execution time и не задерживает
опрос остальных configured volumes.

Capacity rules:

- warning: положительный прирост одного backup превышает 10% места, оставшегося после него;
- critical: hard preflight показывает, что следующий запуск не помещается, либо runtime получил `out of space`;
- фиксированный порог свободных GiB или процентов тома не используется.

### 39.8. SMART collection

SMART собирается внутри preflight каждого executor run через bundled `smartctl.exe`
с JSON output. Отдельной inventory operation, schedule, queue item или job нет.
Сбор охватывает все физические диски из фиксированного executor allowlist, включая
не участвующие в текущей backup job. Adapter:

- обнаруживает устройства через `smartctl --scan-open`;
- получает подробные данные и историю уже выполненных self-tests;
- никогда не запускает SMART short/long/conveyance/offline self-test;
- нормализует ATA/SATA, SCSI/SAS и NVMe в нашу schema;
- сохраняет raw smartctl JSON только в защищённом diagnostics log;
- не публикует serial, WWN и raw device path;
- сохраняет версию smartctl и JSON format version;
- не трактует отсутствующее поле как ноль;
- сохраняет неизвестные vendor attributes только локально;
- использует bounded timeout на каждый диск.

Запуск SMART self-test не входит в v1 ни как scheduled operation, ни как executor
stage, ни как команда `backupctl`. Monitoring остаётся пассивным и сам не создаёт
длительную I/O-нагрузку на диски.

SMART-stage запускается после того, как executor идентифицировал source/destination и
перевёл требуемый backup-диск online, но до VSS и основной data operation. Она сама
не меняет storage topology, mount points или disk lifecycle. Появившиеся внешние
устройства вне allowlist игнорируются. Если configured диск отсутствует или SMART
недоступен, сохраняется последнее наблюдение как `stale/unknown`.

SMART overall `PASSED` не отменяет анализ отдельных counters. Минимальные правила health:

- `critical`: SMART overall failed, NVMe critical warning, pending/uncorrectable/media errors увеличиваются;
- `warning`: появились reallocated sectors, растут interface CRC errors, просрочен self-test или температура превысила configured warning threshold;
- `unknown`: SMART недоступен, устройство/bridge не поддерживается или данные устарели;
- `healthy`: сбор свежий, overall pass и нет warning/critical rules.

SMART monitoring является stateful: manager сохраняет успешные нормализованные
наблюдения в `disk_observations` и сравнивает новое наблюдение не только с
абсолютными правилами, но и с предыдущим сопоставимым наблюдением того же
configured disk. Первое наблюдение создаёт baseline и само по себе не формирует
trend alert. Наблюдение `stale/unknown`, отсутствующее значение и данные другого
физического устройства baseline не заменяют и в сравнении не участвуют.

Ключевыми признаками регресса в v1 считаются:

- переход SMART overall из `PASSED` в failed либо появление NVMe critical warning;
- увеличение ATA reallocated, current pending, offline/reported uncorrectable
  counters;
- увеличение NVMe media/data integrity errors;
- увеличение interface CRC errors;
- ухудшение поддерживаемого устройством нормализованного vendor attribute, если
  adapter однозначно знает направление `worse` для этого attribute.

Для монотонных counters регрессом является положительная дельта, даже если новое
значение ещё находится в vendor-норме и SMART overall остаётся `PASSED`. Снижение
монотонного counter считается reset/wrap/заменой представления, не улучшением:
manager фиксирует диагностическое событие и начинает для него новый baseline без
ложного recovery. Температура, power-on hours, power cycle count и неизвестные
vendor attributes не классифицируются как trend regression только из-за изменения;
для них действуют отдельные абсолютные правила либо требуется явно заданная
семантика adapter. NVMe percentage used сохраняется и показывается как тренд износа,
но его ожидаемый рост сам по себе не создаёт alert без отдельного правила скорости
износа или достижения warning/critical threshold.

Каждый новый регресс ключевого признака создаёт health issue и немедленный Telegram
alert независимо от итоговой severity `warning|critical`. Неизменное значение не
порождает повторный alert при следующем executor run. Следующее увеличение того же
counter является новым регрессом и создаёт новый alert. Дедупликация переживает
restart manager и выполняется по configured disk ID, rule ID, предыдущему и новому
значениям. История наблюдений остаётся источником дельт за 24 часа и 30 дней, чтобы
UI показывал направление и скорость деградации, а не только последний снимок.

Ошибка или незавершённость SMART-stage не является свидетельством неисправности
диска: последнее наблюдение помечается `stale`, SMART-состояние — `unknown`, а общий
system health повышается только до `warning`. Это не изменяет результат backup или
check и не блокирует их запуск. `critical` по SMART выставляется только на основании
успешно полученных метрик, удовлетворяющих критическим правилам выше.

Даже фактический SMART `critical` является диагностикой, а не scheduler/executor
interlock: scheduled backup/check не отменяются, не задерживаются и не заменяются.
Manager немедленно отправляет Telegram alert, но попытка получить свежую копию
продолжается. Job останавливают только наблюдаемая ошибка чтения/записи, потеря
доступа, identity mismatch или нарушение disk lifecycle. Решение не нагружать диск
из-за SMART принимает человек, при необходимости останавливая Windows Service.

Telegram policy для SMART:

- новый critical-condition или любой новый регресс ключевого warning/critical
  показателя — немедленный alert;
- неизменившийся critical не повторяется немедленно, но присутствует в каждом
  суточном heartbeat;
- warning-condition без регресса отдельного alert не создаёт и входит в ближайший
  суточный heartbeat;
- неизменившийся trend regression не создаёт повторных немедленных alerts, но его
  текущее состояние и последняя дельта входят в heartbeat;
- переход warning/critical обратно в healthy после свежего успешного наблюдения
  отражается в heartbeat, без отдельного recovery-сообщения.

Дедупликация немедленного SMART alert выполняется по configured disk ID, rule ID,
предыдущему и новому наблюдаемым значениям/переходу, а не по номеру executor run.

Порог температуры по умолчанию не должен быть единым для всех типов дисков. Сначала используется reported/vendor threshold, затем per-disk config; без них UI показывает температуру без самостоятельной critical-классификации.

### 39.9. Частота обновления

- браузер перечитывает `health.json` и `status.json` каждые 10 секунд;
- manager heartbeat публикуется каждые 5 секунд;
- free space mounted volumes собирается системным monitoring operation каждые 15 минут;
- SMART собирается каждый час без принудительного пробуждения спящего диска;
- после backup executor публикует свежие repository/volume metrics через события данного run;
- полные SMART self-tests не запускаются UI и имеют отдельное расписание будущей версии.

Каждое значение содержит `observed_at`. UI помечает stale по метрике, а не по времени генерации страницы.

### 39.10. Публичная информация

В LAN допустимо публиковать model, роль, capacity, SMART counters, температуры, volume labels, job display names, размеры и расписания. Запрещено публиковать:

- serial/WWN/GUID;
- абсолютные source/repository paths;
- имена файлов и каталогов внутри источников;
- raw restic/smartctl output;
- secrets, usernames и command lines;
- Telegram identifiers.

## 40. Планирование запусков

### 40.1. Cron-правила и циклы jobs

Каждая job независимо содержит собственное cron-правило, timezone и operation
cycle. Scheduler не оперирует недельным планом, общими execution windows или
длительностями jobs. Он вычисляет очередное срабатывание, выбирает текущий элемент
цикла и создаёт ровно один run в назначенное время.

Типичный конфиг может распределять крупные jobs по разным ночам недели:

```text
Пн: library
Вт: photos
Ср: projects
Чт: archive-a
Пт: archive-b
Сб: servers
Вс: repository maintenance / свободное окно
```

Это только результат настройки независимых cron-правил. Job можно назначить хоть
ежедневно, хоть раз в месяц без изменений scheduler. Цикл позволяет стабилизировать
нагрузку: например, `[backup, backup, backup, backup, check]` выполняет проверку
вместо каждого пятого backup, а не дополнительно к нему. Следствием является один
удлинённый интервал между backup-копиями на каждый цикл; это отражается в UI через
следующую operation и учитывается freshness SLA.

Если несколько правил сработали одновременно, runs ставятся в FIFO-очередь по
времени срабатывания, затем по `job_id` для детерминированности. Scheduler не
переставляет их по прогнозной длительности, RPO или приоритету.

Когда новое срабатывание вынуждено ждать уже выполняющуюся другую job, operation
один раз ставится в очередь, а manager немедленно отправляет Telegram-alert о
наложении jobs. Alert содержит текущую job, её стадию и фактическую длительность на
момент наложения, а также job, поставленную в очередь. Это одновременно сигнал о
выходе работ за ожидаемые границы и способ заметить потенциально зависший executor.

Повторное срабатывание job не увеличивает очередь, если operation того же
`job_id + kind` уже находится в `queued` или `running`. Такое срабатывание
фиксируется как `duplicate_trigger_skipped`, но не считается запуском backup,
ошибкой backup или основанием для повторного Telegram-alert о наложении.

Manager не делает вывод о зависании только по фиксированному порогу длительности и
никогда автоматически не останавливает долгий run. Оператор оценивает стадию и
фактическую длительность из UI/Telegram и при необходимости выполняет ручное
действие локальным инструментом управления.

Первоначальный full backup не планируется как обычный ночной запуск: он выполняется отдельной provisioning/initial-seed процедурой в расширенном окне.

### 40.2. Ошибка и следующее срабатывание

Scheduler не создаёт автоматический retry. Если run завершился ошибкой, job ждёт
следующего срабатывания собственного cron-правила. Ошибка остаётся активной в
статусе job и отправляется в Telegram. Ручной повтор возможен только через локальный
`backupctl` и не меняет расписание.

### 40.3. Deadline отдельной job

Job может опционально задавать локальное время deadline для каждого запуска. Это
SLA и условие алерта, а не граница выполнения. Наступление deadline не останавливает
executor и не отменяет run.

В момент фактического выхода run за deadline manager отправляет Telegram alert:

- job и её текущая стадия;
- сколько времени уже занято;
- величина текущего overrun.

Alert дедуплицируется по конкретному run. При продолжительном overrun разрешено
повторное сообщение не чаще одного раза в час. После окончания run отправляется
итог с фактической величиной overrun.

Сам по себе overrun не превращает успешно завершившийся backup в failed или warning.
Нарушение хранится отдельным полем run, чтобы не смешивать целостность backup и
эксплуатационное расписание.
