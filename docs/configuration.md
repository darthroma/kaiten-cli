# Настройка и автоматизация

## Доступ к закрытому репозиторию

Получите доступ у владельца. Если Git ещё не авторизован в GitHub, используйте
GitHub CLI:

```sh
gh auth login
gh auth setup-git
```

## Настройка без агента

Сначала сохраните личный токен:

```sh
kaiten auth login --profile work --tenant company.kaiten.ru
```

Укажите свой домен вместо `company.kaiten.ru`. Токен вводится в скрытом запросе.
Затем найдите нужное пространство и его доски:

```sh
kaiten setup-discovery --profile work --tenant company.kaiten.ru --spaces-only --json
kaiten setup-discovery --profile work --tenant company.kaiten.ru --space 123 --json
```

Замените `123` ID выбранного пространства, а `456` и `789` — ID выбранных досок:

```sh
kaiten setup --profile work --tenant company.kaiten.ru --board 456 --board 789 --confirm --json
kaiten doctor --online --json
```

Повторяйте `--board` для каждой доски. Вновь созданные доски не разрешаются
автоматически.

## Личные настройки

Профили хранятся в `~/.config/kaiten-cli/profiles.json`, выбор досок — в
`~/.config/kaiten-cli/policy.json`. При заданном `XDG_CONFIG_HOME` используется
этот каталог. Обновление пакета сохраняет эти файлы.

Для автоматизации `KAITEN_TOKEN` требует соответствующий `KAITEN_DOMAIN`.
`kaiten auth login --token-stdin` принимает токен из stdin от секретного хранилища.
Значение токена не включается в JSON-ответы.

## Перенос прежних настроек

Для пользователей прежнего `cli-all kaiten`:

```sh
kaiten setup-migrate --json
kaiten doctor --online --json
```

Команда переносит профиль и доски, сохраняя исходные файлы и существующий новый
токен. Отличающаяся новая конфигурация не перезаписывается.

## Обновление

Установить текущую версию из основной ветки:

```sh
uv tool install --reinstall "git+https://github.com/darthroma/kaiten-cli.git"
```

Для закреплённого выпуска выберите существующий тег из
[Releases](https://github.com/darthroma/kaiten-cli/releases).
Скил обновите через `kaiten skill install`; если он был изменён локально,
просмотрите изменения и затем используйте `--force`.

## Скрипты и подробные команды

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

