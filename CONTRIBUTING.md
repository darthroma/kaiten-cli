# Разработка Kaiten CLI

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
сервер, а тесты установленного пакета запускают команду вне репозитория.

Версии в обоих файлах и имя тега должны совпадать.
