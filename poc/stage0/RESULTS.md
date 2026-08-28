# Результаты этапа 0

## Окружение

- Windows 10 Pro, build 19045, x64.
- Python 3.13.5.
- restic 0.19.1, официальный Windows amd64 binary с проверенным SHA-256.

## Проверки

| Проверка | Статус |
| --- | --- |
| Локальный backup/check/restore синтетических данных | Пройдено |
| Unicode, emoji и путь длиннее 260 символов | Пройдено: проверен путь длиной 345 символов |
| Открытый файл без VSS | Пройдено |
| VSS binding и отсутствие orphan | Пройдено на синтетических данных |
| Стабильный namespace через VSS | Пройдено: VSS device prefix отсутствует |
| Online/offline тестового диска | Пройдено через Windows Storage cmdlet |
| Прямой Windows Storage API и восстановление mount point | Пройдено |
| restic fail-fast | Пройдено для source read error |
| restic out-of-space fail-fast | Пройдено по принятому ADR 0001 |
| Принудительное прерывание и recovery | Пройдено для restic repository |

В результаты не включаются имена машины, серийные номера, реальные пути и данные.

Локальный сценарий восстановил и независимо сверил по SHA-256 пять синтетических
файлов. Полный `restic check --read-data` и `restore --verify` завершились успешно.

Администраторский hardware-сценарий подтвердил backup эксклюзивно открытого файла
через VSS, отсутствие нового VSS orphan, стабильный restic namespace, полный check,
restore с независимой сверкой хешей и цикл offline/online выделенного тестового диска.
После теста диск вернулся online и сохранил состояние Healthy.

Fail-fast probe получил машинно-разбираемый source read error во время `archival` и
немедленно отправил cooperative interrupt. Restic завершился с exit code 130, snapshot
не был опубликован, последующий полный check репозитория прошёл. Структурированная
ошибка restic 0.19.1 поступила через stderr: snapshot adapter обязан асинхронно читать
и классифицировать оба потока процесса. Repository write/I/O и out-of-space требуют
отдельных fault-injection проверок.

Elevated Python probe напрямую сопоставил том с PhysicalDrive, подтвердил offline и
online через `DeviceIoControl`, затем назначил дополнительный mount point через
`SetVolumeMountPointW` и сверил Volume GUID. Первый вызов mount API сразу после
возврата диска дал transient WinError 87, повторный запуск прошёл; probe получил
ограниченный retry этого кода на 30 секунд. После проверки диск online/Healthy,
временный mount point удалён.

Out-of-space probe на изолированном VHDX классифицировал первую точную диагностику
stderr за 1.512 секунды и отправил cooperative interrupt. Restic завершился с exit
code 130, временный VHDX был удалён. До прерывания restic не выдал структурированного
JSON error event. ADR 0001 принят: для закреплённой версии разрешён точный,
покрытый integration-тестом классификатор stderr diagnostic.
