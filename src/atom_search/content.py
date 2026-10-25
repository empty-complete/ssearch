"""Editable product identity and complete human messages (stdlib only).

Keep technical package names, data paths, error codes and installer IDs elsewhere.
See docs/development/product-text-catalog.md before adding placeholders or HTML.
"""
from __future__ import annotations

from html import escape
from string import Formatter


PRODUCT = {
    "product_name": "ssearch",
    "product_name_ru": "Simple Search",
    "support_email": "filipp.beloborodoff@yandex.ru",
    "author_name": "Филипп Белобородов",
    "author_title": "разработчик",
    "organization_group": "empty-complete",
    "copyright_holder": "Филипп Белобородов"
}


MESSAGES = {
    "service.invalid_request": "Проверьте параметры запроса.",
    "service.access_denied": "Это действие недоступно для текущего подключения.",
    "service.authentication_required": "Подключение к поиску не подтверждено.",
    "service.service_identity_mismatch": "Подключение к локальному поиску изменилось. Откройте приложение заново.",
    "service.service_unavailable": "Локальный поиск сейчас недоступен. Повторите попытку.",
    "service.service_stopping": "Локальный поиск завершает работу.",
    "service.request_timeout": "Поиск не успел ответить. Повторите запрос.",
    "service.too_many_requests": "Поиск занят. Повторите запрос немного позже.",
    "service.request_too_large": "Запрос превышает допустимый размер.",
    "service.capability_unavailable": "Эта возможность ещё недоступна.",
    "service.project_scope_denied": "Проект недоступен в выбранной области поиска.",
    "service.route_not_found": "Запрошенное действие не найдено.",
    "model.indexing": "Обновляем смысловой индекс: {count}. Поиск по названиям доступен.",
    "model.index_failed": "Смысловой индекс обновлён не полностью. Поиск по названиям доступен.",
    "app.name": "{product_name}",
    "app.title": "{product_name} — {product_name_ru}",
    "app.subtitle": "{product_name_ru} · рабочие документы рядом",
    "app.organization": "{organization_group}",
    "app.company": "{copyright_holder}",
    "app.publisher": "{author_name} (empty-complete)",
    "app.copyright": "Правообладатель: {copyright_holder}. Автор: {author_name}",
    "app.file_description": "{product_name} — локальный поиск документов",
    "startup.failure": "Не удалось запустить {product_name}. Переустановите приложение или обратитесь к автору: {support_email}.{diagnostic}",
    "startup.details": "\nПодробности для поддержки: {path}",
    "migration.title": "{product_name} — перенос данных",
    "migration.busy": "Индекс занят. Закройте Kaizen Search и повторите запуск.",
    "migration.failure": "Не удалось перенести индекс и настройки из {legacy} в {target}. Исходные данные Kaizen Search сохранены. Закройте Kaizen Search, проверьте доступ к папкам и повторите запуск. Причина: {error}",
    "worker.failure": "Не удалось выполнить действие. Проверьте доступ к рабочей папке и повторите попытку.",
    "settings.no_source": "Папка ещё не выбрана",
    "settings.choose_source": "Выберите локальную или сетевую папку в настройках, чтобы начать поиск.",
    "settings.choose_folder": "Выбрать папку…",
    "settings.root_placeholder": "Путь к локальной или сетевой папке",
    "settings.title": "Настройки {product_name}",
    "about.title": "О {product_name}",
    "about.body": "<h2>{product_name} — {product_name_ru}</h2><p>Версия {version} · локальный поиск документов для Windows.</p><p>Автор: <b>{author_name}</b> (empty-complete)</p><p>MIT License · <a href=\"{license_url}\">лицензия приложения</a><br><a href=\"{notices_url}\">Лицензии сторонних компонентов</a></p><p><a href=\"mailto:{support_email}\">{support_email}</a></p>",
    "result.stale_name": "{name}  ·  проверить доступность",
    "result.count": "Найдено: {count}",
    "result.count_limited": "Найдено: {count} · показаны первые 100",
    "scan.progress": "Обновляем список файлов: {count}. Поиск остаётся доступен.",
    "index.count": "В индексе: {count} файлов",
    "updated.at": "Обновлено: {date}",
    "scan.complete": "Список обновлён. {date}",
    "size.bytes": "{number} Б",
    "size.kilobytes": "{number} КБ",
    "size.megabytes": "{number} МБ",
    "size.gigabytes": "{number} ГБ",
    "size.terabytes": "{number} ТБ",
    "result.file_badge": "ФАЙЛ",
    "search.placeholder": "Имя, номер чертежа или смысл запроса…",
    "search.accessible_name": "Поиск по именам и путям файлов",
    "action.refresh": "Обновить",
    "action.cancel": "Отмена",
    "action.settings": "Настройки",
    "action.help": "Справка",
    "search.initial_title": "Найдите нужный файл",
    "search.initial_body": "Введите часть имени, обозначение или описание.\nПоиск использует только имена и пути документов.",
    "result.accessible_name": "Найденные файлы",
    "result.initial_count": "Имена и пути · без чтения документов",
    "search.keyboard_hint": "↑ ↓ выбор    Enter в Проводнике    Esc очистить",
    "index.loading": "Индекс: загрузка…",
    "model.checking": "Смысловой поиск: проверка…",
    "network.unchecked": "Сеть: не проверена",
    "updated.never": "Обновление: ещё не было",
    "network.checking": "Сеть: проверка…",
    "scan.started": "Обновляем список файлов. Поиск остаётся доступен.",
    "reveal.started": "Проверяем файл и открываем Проводник…",
    "settings.root": "Рабочая папка",
    "settings.days_suffix": " дн.",
    "settings.refresh_interval": "Обновлять раз в",
    "settings.note": "Автоматическое обновление работает, пока приложение запущено.\nПосле смены папки нажмите «Обновить». Все настройки хранятся на этом компьютере.",
    "action.save": "Сохранить",
    "action.about": "О программе",
    "action.close": "Закрыть",
    "settings.root_required": "Укажите рабочую папку в настройках.",
    "scan.cancelling": "Отменяем обновление. Текущий список файлов сохранён.",
    "network.scan_failed": "Сеть: ошибка обновления",
    "help.open_failed": "Не удалось открыть справку в браузере. Обратитесь к автору приложения через окно «О программе».",
    "settings.save_failed": "Не удалось сохранить настройки. Проверьте доступ к локальной папке приложения.",
    "settings.invalid_field": "Проверьте значение поля «{field}».",
    "settings.settings_invalid": "Формат настроек неверен. Исходный файл сохранён.",
    "settings.settings_schema_unsupported": "Эта версия настроек не поддерживается. Исходный файл сохранён.",
    "settings.settings_too_large": "Файл настроек превышает допустимый размер 64 КиБ. Исходный файл сохранён.",
    "settings.revision_conflict": "Настройки уже изменились. Откройте их заново и повторите действие.",
    "settings.settings_immutable": "Эту настройку пока нельзя изменить в приложении.",
    "settings.settings_busy": "Обновление ещё завершается. Повторите изменение настроек позже.",
    "settings.registry_warming": "Поиск ещё запускается. Повторите выбор папки через несколько секунд.",
    "settings.source_disabled": "Источник отключён. Выберите включённую папку в настройках.",
    "settings.owned_source_root": "Папка приложения не может быть рабочим источником. Выберите папку с документами.",
    "settings.source_binding_conflict": "Эта папка уже связана с другим источником. Выберите её в настройках заново.",
    "service.source_scope_denied": "Нет доступа к выбранным источникам.",
    "service.result_not_found": "Результат не найден.",
    "service.job_not_found": "Задание не найдено.",
    "settings.settings_apply_failed": "Не удалось применить настройки. Предыдущие значения восстановлены.",
    "settings.settings_recovery_required": "Не удалось завершить восстановление настроек. Файлы сохранены для диагностики.",
    "settings.settings_journal_invalid": "Журнал изменения настроек повреждён. Файлы сохранены для диагностики.",
    "settings.preparation_failed": "Не удалось подготовить локальные настройки. Проверьте доступ к папке приложения и свободное место.",
    "search.empty_title": "Совпадений пока нет",
    "search.empty_body": "Попробуйте часть имени или номер документа.\nЕсли файл появился недавно, обновите список.",
    "index.unavailable": "Индекс: недоступен",
    "model.status_failed": "Состояние модели не получено",
    "settings.scan_in_progress": "Дождитесь окончания обновления или отмените его перед сменой рабочей папки.",
    "model.ready": "Модель: готова",
    "model.unavailable": "Модель: недоступна",
    "model.ready_tooltip": "Локальная модель готова",
    "model.unavailable_tooltip": "Локальная модель недоступна. Поиск по именам и путям работает.",
    "updated.unknown": "Дата обновления неизвестна",
    "network.available": "Сеть: доступна",
    "network.partial": "Сеть: доступна частично",
    "scan.partial": "Обновлена доступная часть папок. Недоступные записи сохранены; проверьте подключение и права доступа.",
    "network.cancelled": "Обновление отменено",
    "scan.cancelled": "Обновление отменено. Ранее найденные файлы сохранены.",
    "network.unavailable": "Сеть: папка недоступна",
    "scan.unavailable": "Рабочая папка недоступна. Проверьте сеть и права доступа. Поиск по сохранённому списку работает.",
    "reveal.outside_source": "Путь находится за пределами выбранного источника.",
    "reveal.invalid_path": "Не удалось определить путь. Проверьте настройки источника.",
    "reveal.access_denied": "Нет доступа к «{path}». Проверьте права доступа к источнику.",
    "reveal.network_unavailable": "Сетевая папка недоступна: «{path}». Проверьте подключение к сети.",
    "reveal.source_missing": "Источник недоступен: папка «{path}» не найдена. Проверьте подключение и настройки.",
    "reveal.shell_failed": "Не удалось открыть Проводник для «{path}». Повторите попытку.",
    "reveal.check_failed": "Не удалось проверить доступность «{path}». Повторите попытку.",
    "reveal.folder_opened": "Открыта папка: «{path}».",
    "reveal.file_selected": "Файл выделен в папке: «{folder}».",
    "reveal.nearest_folder": "Файл не найден: возможно, он перемещён или удалён. Открыта ближайшая существующая папка: «{folder}»."
}


def _render(key: str, values: dict, *, html: bool = False) -> str:
    template = MESSAGES[key]
    fields = {**PRODUCT, **values}
    try:
        names = [name for _, name, _, _ in Formatter().parse(template) if name is not None]
    except (ValueError, TypeError):
        raise ValueError(f"Invalid template for message {key!r}") from None
    for name in names:
        if not name.isidentifier():
            raise ValueError(f"Message {key!r} requires named placeholders")
        if name not in fields:
            raise ValueError(f"Message {key!r} needs placeholder {name!r}")
    try:
        if html:
            fields = {name: escape(str(value), quote=True) for name, value in fields.items()}
        return template.format_map(fields)
    except Exception:
        # Formatting can call a value's __str__/__format__; never echo its errors.
        raise ValueError(f"Invalid template for message {key!r}") from None


def message(key: str, **values) -> str:
    """Render a complete plain-text message using current PRODUCT values."""
    return _render(key, values)


def message_html(key: str, **values) -> str:
    """Render trusted catalog HTML, escaping all inserted values and metadata."""
    return _render(key, values, html=True)
