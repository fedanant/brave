# Облачные runner'ы для сборки Brave

Рекомендуемая начальная схема: GitHub Actions управляет сборками, а три выделенные
облачные машины AWS EC2 выполняют их. В терминологии GitHub это `self-hosted
runners`: собственное железо не требуется, но образы ОС и установку инструментов
нужно обслуживать. Этот документ описывает подготовку инфраструктуры; workflow
сам не арендует и не выключает машины.

## Выбор мощности

Начальная конфигурация для одной одновременной сборки на каждой ОС:

| Сборка | Облачная машина | CPU / RAM | Диск сборки | Метки GitHub |
| --- | --- | --- | --- | --- |
| Linux x64 | EC2 `m7i.8xlarge`, Ubuntu 22.04 x64 | 32 vCPU / 128 GiB | 1 TiB EBS gp3 | `self-hosted`, `Linux`, `X64`, `brave-build` |
| Windows x64 | EC2 `m7i.8xlarge`, Windows Server 2022 x64 | 32 vCPU / 128 GiB | 1 TiB EBS gp3 | `self-hosted`, `Windows`, `X64`, `brave-build` |
| macOS ARM64 | EC2 `mac2-m1ultra.metal`, совместимый macOS AMI | 20 CPU cores / 128 GiB | 1 TiB EBS gp3 | `self-hosted`, `macOS`, `ARM64`, `brave-build` |

Это стартовый запас ресурсов, а не измеренная минимальная конфигурация и не
гарантия времени сборки. Для двух параллельных сборок одной ОС нужна вторая
машина или дополнительная емкость. Характеристики CPU/RAM опубликованы в
[спецификациях EC2](https://docs.aws.amazon.com/ec2/latest/instancetypes/gp.html)
и [описании EC2 Mac](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-mac-instances.html).

Диск: 1024 GiB, gp3, 10 000 IOPS, 400 MiB/s, шифрование EBS. Эти параметры
производительности соответствуют рекомендации AWS для Mac; для Linux/Windows
они выбраны как стартовая конфигурация. Под ОС и инструменты можно использовать
отдельный системный диск. Требуется свободное место **на томе с исходниками**:
пустой диск в 1 TiB не поможет, если рабочий каталог находится на маленьком `C:`.
На Mac исходники должны находиться на APFS.
[Рекомендации AWS для Mac](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-mac-instances.html),
[требования Chromium для Mac](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/mac_build_instructions.md).

ARM64 — начальная архитектура macOS. Для Intel-сборки понадобится отдельный
runner с метками `self-hosted,macOS,X64,brave-build` и выбор `macos_arch=x64`.
Не добавляйте метку `X64` на ARM-машину: изменение метки не меняет архитектуру.

### Почему не стандартные GitHub runner'ы

У GitHub larger runners для Linux/Windows есть конфигурация 32 CPU, 128 GB RAM,
1200 GB SSD. Однако все GitHub-hosted jobs ограничены шестью часами. У macOS
larger runners опубликовано лишь 14 GB рабочего диска. Поэтому они не выбраны
для первой полной сборки всех трех ОС. Self-hosted job может работать до пяти
дней; собственный меньший timeout workflow продолжает действовать.
[Размеры runner'ов](https://docs.github.com/en/actions/reference/runners/larger-runners),
[лимиты Actions](https://docs.github.com/en/enterprise-cloud@latest/actions/reference/limits).

AWS CodeBuild умеет запускать GitHub Actions jobs и допускает timeout до 36 часов.
Но опубликованные стандартные Mac fleets имеют 128/256 GB диска, а таблица
образов для интеграции GitHub Actions не содержит Mac. Это не достаточная
основа для обещания работающей единой схемы CodeBuild для этой сборки.
[Интеграция](https://docs.aws.amazon.com/codebuild/latest/userguide/action-runner.html),
[ресурсы fleets](https://docs.aws.amazon.com/codebuild/latest/userguide/build-env-ref-compute-types.html),
[образы runner'ов](https://docs.aws.amazon.com/codebuild/latest/userguide/sample-github-action-runners-update-yaml.images.html),
[timeout CodeBuild](https://docs.aws.amazon.com/codebuild/latest/userguide/limits.html).

## 1. Подготовить аккаунт и выбрать регион

Для запуска нужны:

- AWS account с доступной оплатой и согласованным бюджетом.
- Регион и Availability Zone с выбранным Mac instance type; наличие типа в
  документации не гарантирует свободный host в нужной зоне.
- Квоты минимум на 64 одновременных On-Demand vCPU семейства M для двух
  `m7i.8xlarge` и на один Dedicated Host выбранного семейства Mac.
- Права администратора репозитория GitHub для регистрации runner'ов.
- Доступ администратора к ОС для установки инструментов и монтирования диска.

Для первой партии используйте On-Demand Linux/Windows. При прерывании Spot
можно потерять текущую компиляцию; переход на Spot имеет смысл после измерения
стоимости и подготовки восстановления. Mac доступен на Dedicated Host с
минимальным сроком аренды 24 часа; это отдельное ограничение от длительности job.
[Условия EC2 Mac](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-mac-instances.html).

## 2. Создать машины и рабочие тома

1. В EC2 создайте Linux и Windows instances по таблице. Установите теги
   `Project=brave-build` и `Platform=linux|windows|macos`, чтобы видеть их расходы.
2. Для Mac откройте **EC2 → Dedicated Hosts → Allocate Dedicated Host**, выберите
   согласованные тип, регион и зону, количество `1`. Затем **Actions → Launch
   instance(s) onto host**, совместимый macOS AMI и тот же host. Выделение host
   запускает платную аренду.
3. Подключите EBS-тома, смонтируйте их и дайте пользователю runner'а право записи.
   В Windows используйте короткий путь без пробелов, например `D:\b` на отдельном
   диске. В Linux — `/opt/brave-build`; в macOS — `/Volumes/build/brave` на APFS.
4. Проверьте реальную емкость файловой системы после изменения размера тома.
   Для Mac увеличение EBS и увеличение APFS-контейнера — отдельные операции.
5. Разрешите исходящий HTTPS к GitHub и источникам зависимостей Brave/Chromium.
   Входящий доступ для назначения jobs не требуется; административный доступ
   к ОС ограничьте своим способом подключения.

[Создание EC2 Mac](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/mac-instance-launch.html),
[увеличение APFS на EBS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/mac-instance-increase-volume.html),
[сеть GitHub runner'ов](https://docs.github.com/en/actions/reference/runners/self-hosted-runners).

## 3. Подготовить образ сборки

На каждой машине заранее установите Git 2.41+, Python **3.12**, Node.js
**24.16.0** и pnpm **11.11.0**. Команда `python` должна запускать Python 3.12
из `PATH` именно аккаунта службы runner'а: workflow использует `shell: python`.
Наличия одной команды `python3` недостаточно. После изменения `PATH`
перезапустите службу и проверьте `python --version` в ее окружении.
Версии Node/pnpm соответствуют текущей конфигурации этого
репозитория; при обновлении закрепленной версии Brave сверяйте его
`package.json`, требования Chromium и проверки build-скрипта.

- **Linux:** Ubuntu 22.04, системные build dependencies и `rpm` для упаковки.
  Выполните подготовку исходников и установку зависимостей по разделу ниже
  **до первой компиляции**. Workflow не запускает `sudo` и не устанавливает
  системные пакеты.
  [Brave Linux prerequisites](https://github.com/brave/brave-browser/wiki/Linux-Development-Environment).
- **Windows:** PowerShell 7 (`pwsh`), Git for Windows, Visual Studio 2022 с
  инструментами C++, ATL/MFC и Windows SDK согласно закрепленной версии Chromium.
  Включите Developer Mode и поддержку длинных путей; используйте короткий путь
  исходников. Все инструменты должны быть доступны аккаунту службы runner'а.
  [Brave Windows prerequisites](https://github.com/brave/brave-browser/wiki/Windows-Development-Environment),
  [Chromium Windows toolchain](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/windows_build_instructions.md).
- **macOS:** установите полный Xcode и выберите его через `xcode-select`,
  завершите первоначальную настройку и принятие лицензии. Версию macOS SDK
  сверяйте с `src/build/config/mac/mac_sdk.gni` выбранной версии Chromium.
  По текущей справке Brave требуется SDK 15.4, например из Xcode 16.3.
  Добавьте рабочий том в исключения Spotlight для скорости индексации.
  [Brave macOS prerequisites](https://github.com/brave/brave-browser/wiki/macOS-Development-Environment).

После установки сохраните идентификаторы AMI и версии инструментов в журнале
первой сборки. Образ ОС с установленным Xcode/Visual Studio существенно
сокращает повторную подготовку. Первая синхронизация Chromium все равно
скачивает большой объем исходников и зависимостей.

### Первая подготовка Linux

Выполните эти действия на облачной Linux-машине под пользователем будущего
runner'а. Git, Python 3.12 под командой `python`, Node и pnpm уже должны быть
установлены. Пользователь должен иметь право записи в каталог EBS.

1. Получите отдельный checkout этого форка с закоммиченными CI-файлами и
   выберите коммит, который будете собирать. Рабочее дерево должно быть чистым:
   `--source-dir .` использует `HEAD` и отклоняет незакоммиченные изменения.
   Сам checkout разместите вне `BRAVE_BUILD_ROOT`.
2. Из корня checkout подготовьте исходники, указав тот же путь EBS, который
   затем запишете в GitHub variable `BRAVE_BUILD_ROOT_LINUX`:

   ```bash
   export BRAVE_BUILD_ROOT=/opt/brave-build
   python scripts/build_brave.py --target-os linux --arch x64 --source-dir . --prepare-only --output "$HOME/brave-prepare-artifacts"
   ```

   Путь `--output` должен отсутствовать или быть пустым и находиться вне
   рабочего дерева сборки. В режиме `--prepare-only` компиляция и экспорт
   артефактов не выполняются. Эта команда скачивает Chromium и инструменты;
   ее нельзя запускать на маленьком системном диске.
3. После завершения выполните команду `sudo`, которую build-скрипт напечатает
   в строке `Sources prepared`. Она содержит точный путь к
   `src/build/install-build-deps.sh` подготовленной версии Chromium. Пройдите
   запросы установки пакетов в административной сессии.
4. Установите пакет для создания RPM:

   ```bash
   sudo apt-get update
   sudo apt-get install -y rpm
   ```

5. Проверьте доступность инструментов под пользователем runner'а:

   ```bash
   python --version
   git --version
   node --version
   pnpm --version
   command -v gcc g++ pkg-config dpkg-deb rpmbuild
   ```

Для режима `upstream-stable` при подготовке опустите `--source-dir .`:
build-скрипт выберет версию из lock-файла. При обновлении Chromium повторите
подготовку и его installer: набор системных зависимостей может измениться.
Последующая компиляция в workflow выполняется без административных прав.
[Порядок установки зависимостей Brave](https://github.com/brave/brave-browser/wiki/Linux-Development-Environment).

## 4. Зарегистрировать runner'ы

В репозитории GitHub откройте **Settings → Actions → Runners → New self-hosted
runner**. Выберите ОС и архитектуру, выполните показанные GitHub команды загрузки
и проверки runner'а. Не копируйте registration token в репозиторий: он временный
и нужен только при регистрации. Добавьте custom label `brave-build`; стандартные
метки ОС, архитектуры и `self-hosted` оставьте включенными.
[Регистрация runner'ов](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners).

Используйте имена `brave-linux-x64-01`, `brave-windows-x64-01` и
`brave-macos-arm64-01`. Один процесс runner'а на машину. Для фоновой работы
настройте штатную службу runner'а по
[инструкции GitHub](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application).

В **Settings → Secrets and variables → Actions → Variables** задайте пути:

| Variable | Значение по умолчанию | Если диск отдельный |
| --- | --- | --- |
| `BRAVE_BUILD_ROOT_LINUX` | `/opt/brave-build` | Путь на смонтированном EBS |
| `BRAVE_BUILD_ROOT_WINDOWS` | `C:\b` | Например `D:\b` |
| `BRAVE_BUILD_ROOT_MACOS` | `/Volumes/build/brave` | Путь на APFS-томе EBS |

Эти значения являются путями, не секретами. Runner'ы должны отображаться как
`Idle` с точными метками из таблицы. Устанавливайте обновления runner application:
GitHub требует обновления в течение 30 дней после выхода новой версии.
[Требования self-hosted runner'ов](https://docs.github.com/en/actions/reference/runners/self-hosted-runners).

## 5. Первая сборка и эксплуатация

1. Выполните локальные проверки конфигурации из [инструкции CI](brave-debloat-ci.md).
2. Запустите workflow вручную сначала для одной ОС. Проверьте выбранные
   исходники, SHA, архитектуру, рабочий том и вывод проверки инструментов.
3. Дождитесь артефактов, проверьте manifest, контрольные суммы, запуск браузера
   и применение политик. Повторите на двух оставшихся ОС.
4. Измерьте время синхронизации, компиляции, максимальный размер рабочего тома
   и итоговую стоимость. После этого можно уменьшать ресурсы или вводить кеши.
5. Сохраните рабочие EBS-тома для последующих запусков. Автоматическое удаление
   исходников и build outputs здесь не настроено; изменение версии и способ
   повторного использования дерева контролирует build-скрипт.

Полный workflow рассчитан на ручной запуск доверенным участником. Проверки PR
должны оставаться на GitHub-hosted runner'ах. Не направляйте непроверенный код
из внешних PR на эти машины: на них сохраняются исходники и состояние сборки.
Регистрация runner'а не требует AWS keys в GitHub Secrets; управление жизненным
циклом EC2 в этой версии выполняется отдельно.

## Расходы и выключение

До аренды рассчитайте в выбранном регионе:

```text
Linux runtime × Linux instance hourly price
+ Windows runtime × Windows instance hourly price
+ оплачиваемое время Mac Dedicated Host × Mac host hourly price
+ EBS storage + provisioned IOPS/throughput + snapshots
+ network/IPv4 + artifact storage
```

Для новой аренды Mac в расчет закладывайте минимум 24 часа. Точное время сборки
пока не измерено, поэтому фиксированная «стоимость одной сборки» не заявляется.
Используйте [AWS Pricing Calculator](https://calculator.aws/),
[EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/)
и [EBS pricing](https://aws.amazon.com/ebs/pricing/) для выбранного региона и ОС.

Завершение или отмена GitHub job **не выключает EC2**. После подтверждения
сохранности артефактов остановите Linux/Windows instances. Остановка прекращает
оплату их compute, но сохраненные EBS продолжают оплачиваться. У Mac остановите
instance, дождитесь доступности host после scrubbing и освободите Dedicated Host
после минимального срока аренды; одна остановка instance не освобождает host.
Не удаляйте рабочие тома без решения о сохранении исходников и результатов.
[EC2 billing](https://aws.amazon.com/ec2/pricing/on-demand/),
[Mac stop/release](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/mac-instance-stop.html),
[EBS billing](https://aws.amazon.com/ebs/pricing/).

Документация проверена 15 сентября 2026 года. Инфраструктура пока не создана,
регион, доступ к AWS и бюджет не заданы; полная сборка на этих машинах не
выполнялась.
