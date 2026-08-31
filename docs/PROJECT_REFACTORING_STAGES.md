# План корректирующего рефакторинга BBS

Статус документа: рабочий план исправлений по результатам полного ревью.

Источник findings: [`PROJECT_REVIEW.md`](../PROJECT_REVIEW.md), ревизия `be21234`.
Нормативные требования по-прежнему задаются `BACKUP_SYSTEM_DESIGN.md`, а критерии
этапов v1 — `BACKUP_SYSTEM_IMPLEMENTATION_STAGES.md`. При конфликте сначала
обновляется нормативный документ, затем код и этот план.

## Цель

Закрыть обнаруженные дефекты совместимости, безопасности, runtime-композиции,
надёжности длинных операций и CLI-контрактов без изменения выбранной архитектуры BBS
и без риска для уже настроенных production jobs.

## Общие правила выполнения

- Этапы выполняются последовательно. Исключение допустимо только для независимого
  критического исправления с явно зафиксированной причиной.
- Каждый этап завершается отдельным тематическим коммитом или небольшой серией
  коммитов, оставляющей `main` в проверяемом состоянии.
- Для каждой исправляемой ошибки сначала добавляется regression-проверка, либо она
  добавляется в том же коммите с исправлением.
- Обязательный программный gate каждого этапа: `ruff check .`, strict `mypy`, полный
  `pytest`. Связанные Windows/hardware acceptance выполняются отдельно, когда этап
  затрагивает соответствующий runtime.
- Разработка и автоматические тесты не читают и не изменяют Stable. Production deploy
  выполняется только отдельной операторской операцией.
- Не деплоить новый runtime во время активной backup/restore/check job. Исключение —
  только аварийное исправление, когда продолжение текущей операции опаснее остановки.
- После изменения runtime-композиции проверяется не только отдельный компонент, но и
  полный путь от production composition root до наблюдаемого артефакта.
- После каждого production deploy проверяются: config validation, успешный restart,
  свежий health heartbeat и доступность Web UI.

## Этап R1 — совместимость, секреты и воспроизводимость

Findings: F-02, F-03, F-09, F-13, F-14.

Статус: **завершён 2026-08-31**. Gates на Python 3.13.5 и Python 3.12.10:
`ruff check .` и strict `mypy` прошли, `pytest`: 405 passed, 8 skipped на каждой
поддерживаемой линии. Python 3.12 acceptance выполнялся в отдельном изолированном
окружении `.poc-work/r1-py312` с версиями инструментов в диапазонах `pyproject.toml`.

### Работы

- [x] Вынести общий cron validator в модульную функцию и использовать её в
   `ScheduleConfig` и `TelegramConfig`, не переоборачивая bound classmethod.
- [x] Исключить значения restic passphrase из `ValidationError`, stderr и
   `bootstrap.jsonl`:
   - хранить passphrase как secret-тип;
   - формировать публичное сообщение validation error без поля `input`;
   - проверить YAML со случайно числовым passphrase.
- [x] Убрать жёсткий 60-секундный порог Web UI:
   - публиковать ожидаемый heartbeat/max-age в `health.json`;
   - рассчитывать его из manager poll interval с безопасным минимальным запасом;
   - использовать опубликованное значение в Web UI.
- [x] Фильтровать deploy source tree: не переносить `__pycache__`, `*.pyc`, тестовые
   caches и другие неописанные runtime-артефакты.
- [x] Заменить production hostname в нормативной документации нейтральным примером.

### Приёмка

- Полный suite проходит как минимум на Python 3.12 и на production-линии Python 3.13.
- Некорректный passphrase не появляется ни в исключении CLI, ни в persisted log.
- Допустимый возраст Web UI heartbeat следует manager-конфигурации.
- Тестовый deploy manifest не содержит Python caches и локальных артефактов.
- Поиск по tracked-файлам не находит известный production hostname.

## Этап R2 — runtime journal и рабочий Logs UI

Finding: F-01. Это главный функциональный пробел рефакторинга.

Статус: **завершён 2026-08-31**. Runtime composition и UI-путь покрыты
integration/contract tests. Gates на Python 3.13.5 и Python 3.12.10: `ruff check .` и
strict `mypy` прошли, `pytest`: 405 passed, 8 skipped на каждой линии.

### Работы

- [x] Подключить `JournalWriter` в production manager composition root.
- [x] Создавать журнал как минимум для событий:
   - старт операции и run;
   - смена stage;
   - progress heartbeat остаётся в SQLite/status projection и не пишется в JSONL;
   - warning/error;
   - terminal result и interruption при startup reconciliation.
- [x] Не записывать credentials, private repository diagnostics, serial numbers и другие
   запрещённые внутренние значения в публичную часть журнала.
- [x] Подключить `LogProjectionPublisher` к тому же потоку событий.
- [x] Публиковать дневные sanitized projections и `logs/index.json` для `web/logs.html`.
- [x] Сохранять установленную календарную retention журнала и атомарность публичных JSON.
- [x] Добавить production-composition integration test:
   `manager run -> journal JSONL -> sanitized daily projection -> index.json`.
- [x] Повторно открыть и закрыть относящийся к журналам критерий Stage 4/Stage 10 в
   acceptance-документации с новой машинной проверкой.
- [x] Улучшить читаемость Status UI без потери диагностических данных:
   - карточки физических дисков по умолчанию отображаются свёрнутыми;
   - в свёрнутом состоянии видны модель, mount points, итоговый статус
     `healthy`/`warning`/`critical` и краткое описание проблемы;
   - производитель, тип, bus, capacity, SMART self-test и таблица метрик скрыты до
     ручного раскрытия карточки;
   - раскрытие работает средствами доступного HTML UI, не требует write API и не
     меняет состояние backend;
   - в карточке каждой job отдельным подписанным полем показывается длительность её
     последнего завершённого запуска; значение не должно быть спрятано внутри общей
     технической строки состояния;
   - длительность форматируется читаемо, включая секунды для коротких jobs, а при
     отсутствии завершённых запусков выводится явное `not recorded`;
   - в карточке job показывается `Repository size` — последний известный физический
     объём всего repository этой job на backup storage;
   - `Repository size` не подменяется логическим размером source, protected bytes или
     количеством данных, добавленных последним run;
   - значение берётся из уже собранных backup metrics после backup/check и не вызывает
     дорогой обход repository при каждом обновлении Web UI; если движок ещё не сообщил
     размер, выводится явное `not reported` вместе со временем последнего измерения,
     когда оно доступно.

### Приёмка

- Завершённая тестовая job оставляет durable JSONL-записи start/stage/result.
- `web/logs.html` загружает непустой `logs/index.json` и выбранный день.
- Public projection не содержит тестовых секретов и внутренних идентификаторов.
- Ошибка log projection не останавливает executor или manager, но наблюдаема.
- Retention удаляет только истёкшие journal days и покрыта тестом.
- После загрузки Status UI каждая disk card компактна и содержит модель, mount points,
  health и причину; полные SMART details появляются после раскрытия.
- Job card явно показывает длительность последнего завершённого run; короткая job не
  отображается как `0h 0m`.
- Job card показывает последний известный физический размер repository целиком и не
  смешивает его с `Protected` или `Added`; отсутствие измерения обозначено явно.

## Этап R3 — надёжность и стоимость длинных операций

Findings: F-06, F-07, F-08, F-10, F-11.

Статус: **завершён 2026-08-31**. Gates на Python 3.13.5 и Python 3.12.10:
`ruff check .` и strict `mypy` прошли, `pytest`: 409 passed, 8 skipped на каждой
поддерживаемой линии.

### Работы

- [x] Ограничить ожидание service start в updater:
   - выводить текущий статус во время ожидания;
   - ввести явный настраиваемый timeout;
   - возвращать различимый ненулевой exit code при timeout/start failure;
   - сохранить ручной запуск сервиса как допустимый deployment workflow.
- [x] Переработать Telegram dispatcher:
   - проверять наличие due notification через существующее manager connection;
   - не открывать и не мигрировать SQLite каждые пять секунд;
   - не создавать HTTP client при пустой outbox;
   - определить и протестировать жизненный цикл HTTP client.
- [x] Оптимизировать restore progress:
   - вычислять total bytes один раз до цикла;
   - ограничить progress events по времени и/или числу файлов;
   - гарантировать первый и финальный progress event;
   - не выполнять SQLite FULL transaction на каждый восстановленный файл.
- [x] Ввести ограничение размера/ротацию `executor-stderr.log` без потери последней
   диагностической информации.
- [x] Хранить cancellation task, наблюдать её исключение и дожидаться завершения при
   shutdown.

### Приёмка

- Updater завершается по timeout и никогда не ждёт бесконечно.
- Пустая Telegram outbox не создаёт дополнительных DB connections или HTTP clients.
- Restore большого synthetic manifest имеет линейную сложность и ограниченное число
  progress transactions.
- Диагностический лог не может расти без ограничения.
- Незавершённая cancellation task отсутствует после cooperative shutdown.

## Этап R4 — завершение CLI-контрактов

Findings: F-04, F-05.

**Статус: завершён.** Полный gate на Python 3.12 и 3.13: 420 tests collected,
412 passed, 8 skipped; `ruff check` и `mypy` проходят на обеих версиях.

### Работы

- [x] Реализовать `backupctl disk status <job-id>`:
   - вернуть структурированный read-only статус;
   - различать unknown job, job без managed disk и ошибку чтения;
   - согласовать поля и exit codes с дизайн-документом.
- [x] Реализовать результат `backupctl queue remove`:
   - команда должна дождаться bounded результата manager;
   - результаты: `removed`, `not_found`, `not_queued`, manager unavailable/error;
   - каждый результат получает документированный exit code;
   - ожидание имеет timeout и не превращается в бесконечный poll.
- [x] Обновить CLI contract tests и operator documentation.

### Приёмка

- Ни одна зарегистрированная CLI-команда не завершается успешным silent no-op.
- Все результаты disk status/queue remove имеют стабильный machine-readable output.
- Manager unavailable и timeout проверяются отдельными тестами.

## Этап R5 — security hardening и уборка

Findings: F-12, F-15 и замечание о форматировании.

### Работы

1. Удалить неиспользуемый `common/json_io.py` и ссылки на него либо обосновать и
   подключить модуль, если он нужен нормативному дизайну.
2. Закрыть ACL window restic password file:
   - создавать файл сразу с restrictive Windows DACL;
   - только после read-back verification записывать passphrase;
   - гарантировать cleanup при ошибке на любом шаге.
3. Добавить Windows security regression test, не использующий production credentials.
4. Отдельным механическим коммитом применить `ruff format` и включить
   `ruff format --check` в обычный gate. Не смешивать форматирование с логикой.

### Приёмка

- Passphrase никогда не существует в файле с унаследованным широким ACL.
- Ошибочные пути создания secret file не оставляют файл на диске.
- Dead module отсутствует или имеет production consumer и тест.
- Ruff format gate проходит на всём репозитории.

## Этап R6 — повторное системное ревью и production acceptance

### Работы

1. Повторить полный repository review с акцентом на composition root и длинные пути.
2. Для F-01—F-15 зафиксировать: закрыто, отклонено с обоснованием или перенесено с
   явным риском и владельцем.
3. Прогнать полный программный gate на поддерживаемых Python versions.
4. Выполнить только необходимые hardware/Stable acceptance после завершения активных
   production jobs.
5. Проверить в Stable:
   - restart validation;
   - свежий status heartbeat;
   - рабочий Logs UI;
   - Telegram failure notification;
   - запуск и завершение тестовой dev job;
   - отсутствие секретов в journal/public projections.
6. Обновить `PROJECT_REVIEW.md` итоговым статусом findings и acceptance-документы.

### Приёмка

- Нет незакрытых High findings.
- Все Medium findings закрыты либо имеют принятое и документированное решение.
- Stable acceptance не использует реальные данные там, где достаточно dev/test data.
- Production jobs и их конфигурации сохраняются при deploy.

## Текущая точка продолжения

На момент создания плана:

- revision `be21234` развёрнута в Stable;
- production job `servers-s` выполняет первый полный backup `S:\` в
  `B:\BBS_DEST\servers-s`;
- во время этой job новый runtime не деплоится;
- следующий шаг разработки — **этап R1**;
- после R1 код можно коммитить и тестировать, но deploy ожидает завершения
  `servers-s`;
- затем выполняется **R2 целиком**, без перехода к остальным findings до получения
  рабочего runtime journal и Logs UI.

## Чек-лист прогресса

- [x] R1 — совместимость, секреты и воспроизводимость
- [x] R2 — runtime journal и Logs UI
- [x] R3 — длинные операции и lifecycle
- [x] R4 — CLI-контракты
- [ ] R5 — security hardening и форматирование
- [ ] R6 — повторное ревью и production acceptance
