# Этап 0: PoC

Проверки этапа выполняются только на синтетических данных. Каталоги `.tools/` и
`.poc-work/` локальны и исключены из Git.

## Зафиксированный restic

Версия и SHA-256 официального Windows-бинарника зафиксированы в
`restic.lock.json`. Архив загружается только со страницы релиза `restic/restic` и
перед распаковкой сверяется с опубликованным `SHA256SUMS`.

Локальный ожидаемый путь:

```text
.tools/restic-0.19.1/restic_0.19.1_windows_amd64.exe
```

Можно передать другой путь через `BBS_RESTIC_EXE`, но исполняемый файл всё равно
должен иметь зафиксированный SHA-256.

## Непривилегированная проверка restic

```powershell
python poc/stage0/restic_local.py
```

Сценарий пересоздаёт `.poc-work/stage0/restic-local`, создаёт синтетические файлы,
включая Unicode и путь длиннее 260 символов, оставляет один файл открытым во время
backup, выполняет `check --read-data`, `restore --verify` и независимо сверяет SHA-256.

Fail-fast и cooperative interruption проверяются отдельно:

```powershell
python poc/stage0/restic_fail_fast.py
```

Probe удерживает синтетический файл без file sharing, читает JSON одновременно из
stdout и stderr restic, прерывает процесс при первом source read error, выполняет
`check --read-data` и подтверждает отсутствие опубликованного snapshot.

## Привилегированные и аппаратные проверки

VSS binding, online/offline и mount-point проверки требуют повышенных прав и
обязательного точного значения `BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID`. Они запрещены
на системном диске и не должны запускаться на носителе с реальными данными.

Первый запуск из PowerShell, открытого от имени администратора, выполняет только
инвентаризацию и проверку закреплённого restic:

```powershell
Set-Location S:\BasovBackupSystem\BBS
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\stage0\admin_preflight.ps1
```

`Bypass` действует только в запущенном процессе и не меняет системную Execution Policy.
Скрипт не меняет состояние дисков и сохраняет JSON в игнорируемый файл
`.poc-work/stage0/admin-preflight.json`. После запуска файл можно исследовать прямо в
рабочей папке; копировать stdout не требуется.

Для полной повторной приёмки на выделенном тестовом диске `D:` используется одна
команда в PowerShell, открытом от имени администратора:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\stage0\run_hardware_acceptance.ps1 -TestDrive D
```

Runner заново выполняет read-only preflight, требует, чтобы `D:` был online,
writable и не являлся boot/system disk, сам устанавливает process-local guard и
запускает только `tests/hardware/test_stage0_windows.py`. Общий результат и полный
stdout/stderr сохраняются соответственно в
`.poc-work/stage0/hardware-acceptance-result.json` и
`.poc-work/stage0/hardware-acceptance.log`.

Отдельный hardware-сценарий при необходимости можно запустить вручную после
установки guard из локального preflight. Пример для `D:`:

```powershell
$preflight = Get-Content .\.poc-work\stage0\admin-preflight.json -Raw | ConvertFrom-Json
$env:BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID = ($preflight.disks | Where-Object { @($_.partitions.drive_letter) -contains 'D' }).unique_id
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\stage0\admin_hardware_test.ps1 -TestDrive D
```

Сценарий повторно сверяет disk number, UniqueId и букву тома, запрещает boot/system
disk, очищает только `D:\bbs-stage0-poc`, проверяет restic через VSS с эксклюзивно
открытым файлом, check/restore/хеши и отсутствие VSS orphan, затем выполняет цикл
offline/online. Результат сохраняется в
`.poc-work/stage0/admin-hardware-result.json`.

Прямой Win32 Storage API проверяется отдельным elevated Python probe с тем же guard:

```powershell
$preflight = Get-Content .\.poc-work\stage0\admin-preflight.json -Raw | ConvertFrom-Json
$env:BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID = ($preflight.disks | Where-Object { @($_.partitions.drive_letter) -contains 'D' }).unique_id
python .\poc\stage0\storage_api_probe.py --drive D
```

Probe сопоставляет том с PhysicalDrive через `IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS`,
переключает offline/online через `IOCTL_DISK_SET_DISK_ATTRIBUTES` и проверяет
`SetVolumeMountPointW` на временной пустой папке `C:\BBSStage0ApiMount`. Результат
сохраняется в `.poc-work/stage0/storage-api-result.json`.

Настоящий out-of-space проверяется на временном изолированном VHDX из elevated
PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\stage0\admin_out_of_space.ps1
```

Тестовый fixture создаётся только в `C:\BBSStage0Faults`, использует динамический
VHDX размером 96 MiB и свободную букву `R:`. Скрипт не меняет реальные разделы или
квоты и отсоединяет VHDX в `finally`. Результат сохраняется в
`.poc-work/stage0/out-of-space-result.json`.

Исчезновение repository во время записи проверяется на отдельном временном VHDX:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\stage0\admin_repository_io.ps1
```

После первого progress helper отсоединяет только созданный им VHDX, классифицирует
первую repository retry diagnostic и cooperative-прерывает restic. Тест ограничен
таймаутами; результат сохраняется в `.poc-work/stage0/repository-io-result.json`.

## Автоматизированный повторный запуск

Обычный `pytest` пропускает внешние и аппаратные PoC. Restic integration включается
через `BBS_RUN_STAGE0_INTEGRATION=1` и запуск `pytest tests/integration`.

Hardware suite разрушителен для содержимого выбранного тестового диска. В elevated
PowerShell он требует одновременно `BBS_RUN_STAGE0_HARDWARE=1`, букву в
`BBS_HARDWARE_TEST_DRIVE` и точный preflight UniqueId в
`BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID`, после чего запускается как
`pytest tests/hardware`. Без всех guard-параметров операции с диском не выполняются.

Hardware suite также проверяет нативный `IVssBackupComponents` backend: создаёт только
`<test-drive>:\bbs-stage0-native-vss\control.bin`, читает его через client-accessible
shadow path и удаляет точный owned SnapshotSet. Другие диски не используются как VSS
source и их состояние не изменяется.
