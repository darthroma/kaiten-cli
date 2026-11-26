# Kaiten CLI

Работа с Kaiten из терминала и чата агента:

- Создание карточек с описанием, сроком и ответственным.
- Редактирование названия, описания, срока, типа и пользовательских полей.
- Поиск по тексту, участникам, меткам, расположению, срокам и архивному состоянию.
- Просмотр истории, отправка карточек в архив и восстановление из архива.
- Внутренние комментарии с упоминанием коллег и вложениями.
- Загрузка файлов прямо в карточку.
- Поиск людей, назначение ответственных и управление участниками.
- Управление метками, чек-листами, связями и блокировками.
- Перенос между разрешёнными досками, массовые правки и архивация.
- Выгрузка поиска в CSV или JSON, отчёты по просроченным и заблокированным задачам и по ответственным.

## Установка

Нужны [uv](https://docs.astral.sh/uv/getting-started/installation/) и Git.

```sh
uv tool install "git+https://github.com/darthroma/kaiten-cli.git"
kaiten skill install
```

## Подключение Kaiten

Получите API-токен своего аккаунта Kaiten и выполните в терминале:

```sh
kaiten auth login --profile work --tenant company.kaiten.ru
```

Вместо `company.kaiten.ru` укажите домен вашей компании. Токен вводится скрыто.

Откройте новый чат агента и попросите:

> $kaiten-cli Подключи мой Kaiten: профиль work, домен company.kaiten.ru.
> Покажи доступные пространства и помоги выбрать доски для работы.

Агент покажет список и сохранит выбранные вами доски. Если работаете без агента,
используйте [инструкцию настройки](docs/configuration.md#настройка-без-агента).

## Использование в чате

После настройки можно просить агента:

> Создай карточку «Проверить выгрузку» на доске разработки, в колонке «Очередь».

> В этой карточке обнови описание и перенеси срок на 20 октября.

> $kaiten-cli Покажи карточку по этой ссылке: …

> В этой карточке оставь комментарий «Готово» и отметь коллегу @username.

> Прикрепи этот PDF к комментарию «Отчёт готов» и отметь коллегу @username.

> Загрузи этот Excel во вложения карточки.

> Перемести эту карточку в колонку «В работе».

Агент найдёт карточку, выполнит разрешённое действие и проверит результат.

## Использование в терминале

```sh
kaiten cards view 12345 --board 456
kaiten cards view 12345 --board 456 --with-related --json
kaiten cards search 'Отчёт' --board 456
kaiten cards search --board 456 --responsible 789 --condition active
kaiten cards archive --board 456
kaiten cards update 12345 --board 456 --archive --dry-run
kaiten cards update 12345 --board 456 --archive --authorization direct
kaiten cards update 12345 --board 456 --restore --authorization direct
kaiten cards history 12345 --board 456
kaiten boards info 456
kaiten users find @username
kaiten members assign 12345 @username --board 456 --dry-run
kaiten cards bulk-update 12345 12346 --board 456 --archive --dry-run --json
kaiten cards report --board 456 --kind overdue --json
kaiten cards export --board 456 --condition active > cards.csv
kaiten files upload 12345 --board 456 --file ./report.pdf --authorization direct
```

Замените числа своими ID. Доступные команды — `kaiten --help`.
Для скриптов добавляйте `--json`. В выводе `cards view`, `cards search` и
`cards archive` доступны участники и их роли, сроки, метки и другие поля карточек.

`cards view` показывает прямых родителей (`parents`) и детей (`children`):
ID, название, доску и ссылку. `--with-related` добавляет описание и другие
доступные поля каждой из этих карточек. Откройте нужную связь через
`cards view <ID> --board <board_id>`, чтобы пройти на следующий уровень.
`relations_complete: false` означает, что часть связей недоступна.

CLI работает в пределах выбранных досок. При архивации из очереди или работы
Kaiten может удалить связи с родительскими и дочерними карточками. Если
предпросмотр показывает этот эффект, для записи требуется `--allow-unlink`.

Примеры команд записи, отчётов и переноса между досками есть в
[справке по работе с карточками](docs/configuration.md#участники-и-поля-карточки).

[Настройки, обновление и автоматизация](docs/configuration.md) ·
[Разработка](CONTRIBUTING.md)
