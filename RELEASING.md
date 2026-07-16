# Выпуск версии

## 1. Подготовка

1. Обновите `version` в `pyproject.toml`.
2. Перенесите изменения из `[Unreleased]` в версионный раздел `CHANGELOG.md`.
3. Убедитесь, что в рабочем дереве нет `.env`, TSV, логов и токенов.

## 2. Локальная проверка

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest -q
python -m build
python -m twine check dist/*
```

Дополнительно проверьте установку wheel в чистом окружении и импорт пакета без токена:

```bash
python -m venv .release-venv
.release-venv/bin/python -m pip install dist/*.whl
.release-venv/bin/python -c "import yadirect_mcp; print('import ok')"
```

На Windows замените `.release-venv/bin/python` на `.release-venv\Scripts\python.exe`.

## 3. Проверка GitHub-репозитория

Проект опубликован в `Lermont/yamcp`. Проверьте remote и синхронизацию ветки:

```bash
git remote get-url origin
git status --short --branch
git push origin main
```

Через GitHub CLI можно задать поисковое описание, homepage и Topics:

```bash
gh repo edit Lermont/yamcp \
  --description "MCP server for Yandex Direct reports and guarded campaign setup" \
  --homepage "https://github.com/Lermont/yamcp" \
  --add-topic mcp \
  --add-topic model-context-protocol \
  --add-topic yandex-direct \
  --add-topic ppc \
  --add-topic ai-agents \
  --add-topic python
```

Перед выполнением проверьте активный аккаунт через `gh auth status`. То же самое можно сделать в **Settings → General → Repository details**.

## 4. Тег и GitHub Release

```bash
git tag -a v0.2.0 -m "yadirect-mcp v0.2.0"
git push origin main --tags
gh release create v0.2.0 dist/* --verify-tag --generate-notes --title "yadirect-mcp v0.2.0"
```

После публикации проверьте README, архив исходников, wheel, лицензию и инструкции чистой установки на Windows или Linux.
