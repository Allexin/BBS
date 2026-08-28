# Результаты этапа 7

## Окружение

- Windows 10 Pro, x64.
- Python из проектного virtual environment.
- Закреплённый restic 0.19.1 Windows amd64.
- Одноразовые данные и repositories только на выделенном тестовом диске D.

## Проверки

| Проверка | Статус |
| --- | --- |
| Backup и обязательный retention | Пройдено |
| Repository без пароля (`--insecure-no-password`) | Пройдено |
| Password-protected repository через временный password file | Пройдено |
| Детерминированный subset check `1/4` | Пройдено |
| Отдельный prune | Пройдено |
| Обнаружение повреждения repository полным check | Пройдено |
| Блокировка следующего backup verification gate | Пройдено |
| Fail-fast source/out-of-space через реальную process group | Пройдено |

Оба repository получили по два snapshots. Обязательный retention оставил ровно одну
точку восстановления в каждом. После prune pack-файл каждого изолированного
repository был намеренно повреждён; полный check обнаружил повреждение, а следующий
backup был отклонён до доступа к restic.

Результат hardware-прогона сохраняется локально в
`.poc-work/stage7/snapshot-hardware-result.json`; идентификаторы snapshots и временные
пути в репозиторий не добавляются.
