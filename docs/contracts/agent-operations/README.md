# Контракты настроек и примеры API диагностики

Эти файлы — **учебные примеры проектного контракта**, не активные настройки,
реальный health или отправленный issue. Все данные с `example: true` вымышлены;
`C:\Example\Documents` не является предложением добавить эту папку.
Настройки schema3, authenticated loopback HTTP v1, общая очередь metadata-first
и cached health реализованы в исходниках линии разработки 0.2.0; приёмка готовой
сборки учитывается отдельно. MCP, ремонт, обновления и outbox ещё не реализованы.
Учебный JSON не является конфигурацией приложения или результатом проверки ПК.

Чистый модуль [repair](../../../src/atom_search/repair/) содержит предварительный
source-only срез политики и неизменяемых планов. Он не подключён к HTTP/MCP,
не выдаёт capabilities, не применяет ремонт и не меняет существующий writer.
`AgentPrincipal` создаётся только доверенным владельцем из существующего
`AccessContext` и текущей revision политики; JSON клиента не создаёт principal.
По умолчанию политика выключена и actions пусты. Зарезервированный `agent_repair`
в этом модуле — будущее явное право, текущий сервис его не выдаёт; `owner` сам
по себе не заменяет его. UUID scope проверяется явно, пустой scope не wildcard.

[repair-plan.schema.json](repair-plan.schema.json) описывает только планы
`SOURCE_UNREACHABLE` / `SOURCE_ACCESS_DENIED`: cached факт не старше 300 секунд,
один `retry_source_probe` / `pause_source_job`, максимум 5 секунд и одна попытка.
План связывает principal, policy/config revisions, index generation, UUID источника
и SHA-256 канонического cached факта, живёт 600 секунд. `observed_at` ответа не
освежает факт. Preflight требует точную ссылку `health:<health_fact_hash>` и
отвергает изменённые descriptors или устаревшие условия. Это проверка доверенного
in-process DTO; будущий apply должен разрешать только ID сохранённого сервером
плана с durable ключом через единственный coordinator. Принимать клиентский DTO
как разрешение на выполнение нельзя. Пока `idempotency_key=null`, apply отсутствует.
`peak_extra_bytes=0` описывает этот чистый срез без действий, а не резерв места для
будущего ремонта/журнала. `not_required` не обещает rollback. Global/runtime коды
возвращают `unsupported_repair` до проверенных owner/scope/storage/recovery ports.

Локальная `RepairPolicy.settings_validate` переиспользует существующий
`MutationCoordinator.validate` как доверенный чистый port. Сохраняется действующее
P2 требование capability `owner`; дополнительно нужны включённая текущая политика,
явный scope всех включённых источников и owner bounds внутри schema3
`86400..31536000` для единственного поля `indexing.interval_seconds`.
Возвращаемая `AgentSettingsValidation.normalized` — только безопасный patch,
не полный owner config и не кандидат apply; paths/credentials из owner snapshot
не возвращаются. Новый settings API, permission grant или mutation writer не добавляются.

- `settings.example.json`: формат/монотонная revision, источники и профили,
  целевой managed PostgreSQL (без credentials),
  квота, UI/theme/hotkey, updates, onboarding/greeting, search/project_aliases,
  confirmed projects/branch bindings, ограниченная политика агента и outbox; адрес назначения пока `null`.
  Формат совпадает с текущей формальной schema3: UUID источников,
  `database.mode`, `ui.global_hotkey`, фиксированный `engine/postgresql/18/data`.
  Для согласованности примера владелец заранее разрешил только перечисленные
  действия ремонта; в новом реальном профиле агентский ремонт по умолчанию выключен.
  Вымышленные проект/алиас показывают хранение подтверждённых сопоставлений;
  Project retrieval и scoped aliases реализованы. Их capabilities доступны после
  подтверждения готовности БД и проекции текущей revision, с учётом разрешённых
  источников; [cached readiness checks](../../../src/atom_search/service/health.py)
  не подменяются наличием настроек.
  Physical schema1/schema2 и их journal/backups читаются только для восстановления и
  однократной миграции через owner coordinator; публичный apply принимает schema3.
  `''` привязывает весь источник, обычный prefix сопоставляется по сегментам;
  `ПМ` не включает `ПМ-2`. Предложения с `confirmed:false` не применяются.
- `health.example.json`: вымышленный пример текущего `HealthSnapshot` в HTTP v1
  envelope. Внешний `example: true` и ключ `response` принадлежат только учебной
  обёртке; на проводе передаётся объект `response`. UUID, даты и все числа
  вымышлены, включая маленькие размеры для проверки арифметики.
  Источники/задания находятся в `data.components`, неизвестные счётчики/байты
  и знаменатель — `null`, проверенный ноль остаётся `0`. SQL catalogue/current
  compatible model counts не равны прогрессу задания; их время проверки отдельно
  от доступности источника. Health читает память, возраст ровно 300 секунд ещё
  свежий; более старый факт помечается `stale`. Scope применяется до агрегации.
  PGDATA physical size включает WAL/temp; SQL logical size и разбиение WAL
  не прибавляются повторно к physical_owned_bytes; outstanding reservations
  также отдельны. Неизвестный ledger означает `reserved_bytes: null`.
  Outbox 0/0/null/false — placeholder до реализации доставки; capability ремонта/outbox/deep_content
  отсутствует. Сохранённая policy не включает будущую возможность.
- `incident.example.json`: локальная запись с неизменяемым `report` и отдельным
  `delivery`. В общей папке публикуется только канонический `report` под его UUID;
  queued/sent, попытки и адрес — локальное состояние доставки, не факт ремонта.

Текущие типы — [service contracts](../../../src/atom_search/service/contracts.py),
маршруты — [HTTP server](../../../src/atom_search/http/server.py),
проверки scope и owner operations — [application API](../../../src/atom_search/service/application.py).
Для health/incident ещё нужны отдельные формальные JSON Schema; health DTO/client
roundtrip проверяется отдельно от будущего incident API.
Рабочий settings writer отвергает `example: true`; валидация учебного примера явно
извлекает payload без marker, входящий apply marker автоматически не удаляет.
Числа лимитов
являются стартовыми проектными решениями, не измеренными оптимумами.
Settings — полный учебный schema3 payload. Job state/counters
относятся к health; сохранённые просмотры/этапы обучения — отдельное состояние.

## Рабочие настройки schema3

Формальная схема: [settings-v3.schema.json](../../../resources/schemas/settings-v3.schema.json).
Все объекты закрыты, неизвестные поля и plaintext credentials запрещены; credential
references допустимы только в объявленных полях. Файл `<profile>/settings.json`
ограничен 64 КиБ. Источники имеют постоянные UUID и неповторяющиеся нормализованные
пути; в конфигурации от 0 до 64 источников. Текущий desktop редактирует один источник,
сохраняя остальные; общий scheduler уже обслуживает все включённые источники.

`validate_settings` проверяет schema без probe/записи и возвращает независимую копию.
`MutationCoordinator.validate` дополнительно проверяет ожидаемую revision и diff.
`owner_apply(candidate, expected_revision)` сериализует owner-изменения; candidate
содержит текущую или следующую revision, окончательную назначает writer. Устаревшая
revision даёт `revision_conflict` (409), незавершённое несовместимое задание после
кооперативной отмены до 5 секунд — `settings_busy` (409). Поиск/статус не ждут этого
mutation lock. Ошибка замены до commit сохраняет старую revision и активный snapshot.
Отказ component hook после commit восстанавливает значения с новой revision:
8 → 9 → 10; клиент получает `settings_apply_failed`.

Под тем же host guard и whole-engine budget хранятся ограниченные backup:
`engine/settings-backups/legacy.json` (точные bytes legacy миграции), `schema1.json`/
`schema2.json` (оригиналы при обновлении формата) и `last-good.json` (предыдущий
валидный snapshot). [Settings repository](../../../src/atom_search/settings/repository.py)
управляет этими файлами. Единственный журнал
`engine/runtime/settings-operation.json` ограничен 1 МиБ, содержит
`schema_version:1, operation_id:UUID, state, before, candidate`; состояния:
`prepared`, `committed`, `applied`, `rolled_back`. После незавершённой операции load
сначала откатывает неподтверждённый candidate; если он достиг диска, revision растёт.
Повреждённый/неизвестный config или журнал сохраняется, запросы не принимаются до
восстановления. Это не реализация будущего сервиса backup/recovery.

Legacy `root/refresh_days` переносится один раз без подмены выбранной папки, UUID
сохраняется при повторном запуске. `last_attempt/last_refresh` отделены в
`engine/runtime/desktop-state.json`, привязаны к `source_path`; обход не увеличивает
settings revision. Новый профиль получает `sources: []`, без сетевого probe/автоматических catalog/model jobs до выбора папки; deep всегда `off`.
Schema1/2 мигрируют один раз через тот же owner coordinator: значения, UUID источников и update choices сохраняются, revision растёт на один; исходные bytes — `schema1.json`/`schema2.json`.
Новый `updates.source` указывает на GitHub Releases empty-complete/ssearch с точными origins и без credential; это конфигурационный slot, download/apply capabilities пока отсутствуют.
При reopen действует фактическая canonical policy `engine/settings.json` локального runtime.
`storage` отсутствует в `mutable_fields`: quota/min_free изменения отклоняются до
реализации согласованного policy hook, без нового ledger/бюджета или relocation.
Сохранение UI/update/repair slots не включает соответствующие capabilities.
