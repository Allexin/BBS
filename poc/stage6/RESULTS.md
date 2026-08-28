# Stage 6 mirror acceptance

Дата проверки: 2026-08-28.

## Software acceptance

- Полный набор: 239 passed, 7 guarded tests skipped.
- Ruff: passed.
- mypy strict: passed.
- Fault tests подтверждают fail-fast preflight, durable tombstones, interrupted
  publish recovery, verification gate и manual repair.
- Full check обнаруживает missing, unexpected, size и content corruption.

## Guarded Windows hardware acceptance

Команда:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\stage6\run_mirror_acceptance.ps1
```

Результат: 1 passed. На явно разрешённом disposable-диске проверены нативные
`CopyFile2`, flush, атомарная замена существующего файла и полная SHA-256 проверка.
Созданный тестом уникальный каталог удалён cleanup; другие диски не изменялись.
