# Kaiten CLI

Самостоятельный клиент официального REST API Kaiten для терминала, скриптов и
агентов. Этот репозиторий содержит только Kaiten: собственный пакет, тесты,
документацию, скил и выпуск. Он не зависит от фабрики CLI или других сервисов.

## Установка

Нужны [uv](https://docs.astral.sh/uv/getting-started/installation/) и Git.
Для этого закрытого репозитория получите доступ у владельца и настройте GitHub:
`gh auth login`, затем `gh auth setup-git`. Токены не вставляются в URL.

```sh
uv tool install "git+https://github.com/darthroma/kaiten-cli.git@v0.1.0"
kaiten --version
kaiten --help
kaiten skill install
```

Python-пакет называется `kaiten-agent-cli`, команда — `kaiten`. Требуется Python
3.11 или новее; uv умеет установить подходящий Python. Команды работают из любой
папки. CI проверяет Linux и macOS. Скил `kaiten-cli` устанавливается в
`~/.agents/skills/kaiten-cli/SKILL.md`; вызов в новом чате — `$kaiten-cli`.
Для другого каталога используйте `kaiten skill install --directory /path/to/skills`.
Изменённый локальный скил сохраняется; просмотрев отличия, обновите с `--force`.

## Личный доступ к Kaiten

```sh
kaiten auth login --profile work --tenant example.kaiten.ru
kaiten setup-discovery --profile work --tenant example.kaiten.ru --spaces-only --json
kaiten setup-discovery --profile work --tenant example.kaiten.ru --space 123 --json
kaiten setup --profile work --tenant example.kaiten.ru --board 456 --confirm --json
kaiten doctor --online --json
```

Замените домен и IDs своими. Повторяйте `--board` для нескольких выбранных досок.
Токен вводится в скрытом запросе. Профили хранятся в
`~/.config/kaiten-cli/profiles.json`, разрешённые доски — в
`~/.config/kaiten-cli/policy.json` (или внутри `XDG_CONFIG_HOME`). Токены и личные
настройки не входят в пакет. Для автоматизации `KAITEN_TOKEN` требует совпадающий
`KAITEN_DOMAIN`; также поддерживается явный `auth login --token-stdin`.

Если ранее использовали `cli-all kaiten`, достаточно один раз выполнить:

```sh
kaiten setup-migrate --json
kaiten doctor --online --json
```

Перенос сохраняет выбранный профиль и доски, не показывает токен и не меняет
исходные файлы. Отличающиеся новые настройки не перезаписываются. Последующая
работа не обращается к фабрике или её настройкам.

## Карточки, комментарии и перемещение

```sh
kaiten cards view 12345 --board 456 --json
kaiten cards search 'Отчёт' --board 456 --json
kaiten boards info 456 --json
kaiten comments list 12345 --board 456 --json
```

Допустимы ID карточки или HTTPS-ссылка на неё в настроенном домене. Если доска
известна, указывайте `--board` для сокращения перебора.

Комментарий с явно выбранным упоминанием:

```sh
kaiten comments prepare 12345 --board 456 --text 'Готово' --mention 'Имя Фамилия' --json
kaiten comments post 12345 --board 456 --text 'Готово' --mention 'Имя Фамилия' --approval '<hash из prepare>' --json
```

Для комментария без упоминания используйте `--mention нет`. CLI отправляет только
внутренние комментарии и проверяет автора, текст, новый ID и `internal=true`.

Перемещение по точному запросу пользователя:

```sh
kaiten move prepare 12345 --board 456 --column 789 --json
kaiten move apply 12345 --board 456 --column 789 --authorization direct --json
```

Для предложения агента требуется согласие «перемещай» и подготовленный
`--approval`. Полоска сохраняется, если не задана другая через `--lane`.
Действия поддерживают `--dry-run`; разрешённое действие завершается записью и
проверкой результата. Все записи выполняются один раз без автоматических
повторов. Неопределённый результат требует чтения текущего состояния.

`--json` возвращает одну запись `{ok,data,error}` и ненулевой код при ошибке.
Поиски ограничены 20 страницами по 100 карточек на доску; проверяйте `complete`.
Текстовый поиск не выбирает назначенные человеку задачи: фильтр исполнителя,
участника или статуса пока отсутствует. Нет удаления, администрирования,
перемещения между досками или внешних комментариев.

## Обновления и разработка

```sh
uv tool install --reinstall "git+https://github.com/darthroma/kaiten-cli.git@v0.1.1"
```

`v0.1.1` — пример следующего выпуска; выбирайте существующий тег. При обновлении
личные настройки сохраняются. Скил обновляется отдельной командой после
просмотра локальных изменений.

```sh
gh repo clone darthroma/kaiten-cli
cd kaiten-cli
uv sync --locked
uv run pytest
uv build
```

Для выпуска обновите версию в `pyproject.toml` и `src/kaiten_cli/__init__.py`,
`CHANGELOG.md` и `uv.lock`, отправьте изменения в `main`, затем совпадающий
версионный тег. GitHub Actions проверит тесты и соберёт wheel и исходный архив.
Тесты не выполняют записей в рабочий Kaiten; HTTP-проверки используют локальный
сервер, а тесты wheel запускают команду вне checkout без зависимости от фабрики.
