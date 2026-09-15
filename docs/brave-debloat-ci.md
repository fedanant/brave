# Brave + debloatinator в GitHub Actions

Workflow **Build Brave with debloat policies** компилирует Brave из исходников
для Windows x64, Linux x64 и macOS ARM64 либо Intel. Результат каждой job —
артефакт с браузером, отдельным набором политик и данными о сборке.

Политики необходимо установить по README из артефакта. Они отключают функции
Brave, но не удаляют их код из браузера. Компиляция не делает debloatinator
встроенной модификацией исходников. Сборки не подписаны вашим сертификатом;
Apple notarization и автоматические обновления не настроены.

## Что требуется для первого запуска

1. Подготовить облачные машины по [инструкции runner'ов](cloud-runners.md).
   GitHub Actions выполняет jobs на этих машинах; workflow не арендует их.
2. Зарегистрировать runner'ы с меткой `brave-build` и нужными ОС/архитектурами.
3. Проверить пути `BRAVE_BUILD_ROOT_*` в **Settings → Secrets and variables →
   Actions → Variables**. Рабочее дерево Chromium хранится на большом
   постоянном диске, отдельно от checkout GitHub Actions.
4. Открыть **Actions → Build Brave with debloat policies → Run workflow**.
   Выбрать ветку, `target_os`, `macos_arch` и `source`.
5. После успешного завершения скачать **Artifacts → brave-debloat-…**.

Workflow с `workflow_dispatch` должен быть добавлен в основную ветку, чтобы
GitHub показывал кнопку **Run workflow**. После этого в форме можно выбирать
другую ветку для конкретной сборки.

Полная компиляция запускается только вручную. Push и PR запускают отдельный
workflow **Validate Brave debloat CI** на обычных GitHub runner'ах: проверка
workflow, тесты скриптов и dry-run без скачивания Chromium.

## Выбор исходников

| `source` | Что компилируется |
| --- | --- |
| `repository` (по умолчанию) | Коммит выбранной ветки этого форка, полученный Actions checkout. Изменения браузера в форке учитываются. |
| `upstream-stable` | Версия из `config/build-lock.json`: upstream Brave `v1.95.101`, закрепленный commit. |

Режим `repository` берет закоммиченные исходники; незакоммиченные изменения
локальной машины не отправляются в CI. Версии Chromium и Brave определяются
по `package.json` выбранного коммита. Перед обновлением исходников сверяйте
версию Node/pnpm и требования инструментов с lock-файлом.

`brave-debloatinator` закреплен на коммите
`7ebd2d1b109e73e650878d5b8202ea282a083d81`. Исходный JSON хранится в
`policies/upstream/`, его checksum проверяется при упаковке. Linux installer
из upstream не запускается: он устанавливает политики на текущую машину.

## Артефакты и обновления

В `browser/` попадают установочные пакеты и ZIP: Windows `.exe`, Linux
`.deb`/`.rpm`, macOS `.dmg`/`.pkg`. `build-manifest.json` содержит версии,
фактические коммиты Brave/Chromium/depot_tools, параметры и SHA256 пакетов;
корневой `SHA256SUMS` покрывает браузер, manifest и политики.

Linux-пакеты upstream зависят от `brave-keyring`; этот пакет нужно обеспечить
отдельно перед установкой `.deb`/`.rpm`. ZIP также включен в артефакт.
Не считайте успешное создание пакета проверкой установки на чистую ОС.

Автообновление браузера отключено параметрами GN, чтобы официальный updater
не заменял пользовательскую сборку. Обновления безопасности нужно получать
самостоятельно: обновить исходники/lock, снова собрать и установить браузер.
Эта конфигурация не создает собственный сервер обновлений.

Первое рабочее дерево требует не менее 600 GiB свободного места; повторная
сборка использует сохраненное дерево. Скрипт не удаляет старые исходники и
пакеты. Каталог `--output` должен быть новым либо пустым.

## Политики

Пакет содержит Windows `.reg`, Linux managed JSON, macOS plist и configuration
profile `.mobileconfig`. Профиль macOS строится для фактического
`CFBundleIdentifier` собранного приложения. Установка и откат описаны в
`policies/README.md` внутри артефакта.

Набор отключает Rewards, Wallet, VPN, Leo, Tor, встроенный менеджер паролей и
другие перечисленные в исходном JSON функции; задает страницу новой вкладки
`https://search.brave.com/`. Политика страницы новой вкладки из Linux JSON
применяется также к Windows и macOS. `BraveVPNDisabled: 1` преобразуется в
логическое `true` для JSON/plist.

Это управляемые настройки: пользователь не сможет включить заблокированную
функцию обычным переключателем, пока действует политика. Политики могут
затронуть другую установку Brave с тем же policy domain. Перед установкой
используйте инструкции резервного копирования из пакета.

## Локальные проверки без компиляции

Из корня репозитория с Python 3.10+:

```text
python -m unittest discover -s scripts/tests -v
python scripts/build_brave.py --target-os windows --arch x64 --dry-run
python scripts/build_brave.py --target-os linux --arch x64 --dry-run
python scripts/build_brave.py --target-os macos --arch arm64 --dry-run
python scripts/package_policies.py --output dist/policy-preview
```

Для проверки коммита форка добавьте `--source-dir .` к build-команде.
`--dry-run` показывает план, не скачивает Chromium и не проверяет наличие
компилятора. Для отдельной генерации macOS-политик можно задать
`--macos-bundle-id`; при полноценной сборке идентификатор читается из `.app`.

Проверка YAML с [actionlint](https://github.com/rhysd/actionlint):

```text
actionlint .github/workflows/build-brave-debloat.yml .github/workflows/validate-brave-debloat.yml
```

## Проверка результата на каждой ОС

1. Сверить контрольные суммы артефакта и версии/коммиты в manifest.
2. Установить или распаковать браузер из артефакта и проверить запуск.
3. Сохранить предыдущие политики и установить файлы для своей ОС по README.
4. Открыть `brave://policy`, обновить политики и проверить отсутствие ошибок
   типов/неизвестных ключей. Затем перезапустить браузер.
5. Проверить скрытие Rewards, Wallet, VPN, Leo и Tor, поведение менеджера
   паролей и новой вкладки. Проверить обычную загрузку сайтов и Shields.
6. Проверить откат политик и восстановление предыдущих значений.

Успешная проверка скриптов не заменяет компиляцию и запуск браузера на трех ОС.
Первая облачная сборка и применение политик должны быть проверены отдельно.

## Основные файлы

- `.github/workflows/build-brave-debloat.yml` — ручная компиляция и артефакты.
- `.github/workflows/validate-brave-debloat.yml` — быстрая проверка CI.
- `scripts/build_brave.py` — синхронизация, сборка и упаковка.
- `scripts/package_policies.py` — генерация политик без установки на runner.
- `scripts/ci_matrix.py` — выбор ОС, архитектуры и облачной машины.
- `config/build-lock.json` — версии upstream и инструментов.
- `policies/upstream/SOURCE.json` — происхождение политик.

Система сборки сверена с [Brave Core](https://github.com/brave/brave-core) и
его [документацией](https://github.com/brave/brave-browser/wiki).
Управляемые настройки описаны в
[Brave Group Policy](https://support.brave.app/hc/en-us/articles/360039248271-Group-Policy).
