# ssearch Core

Самостоятельный Python-пакет `ssearch-core` для ssearch и других клиентов. Базовый каталог SQLite и поиск по названиям/путям используют только
стандартную библиотеку Python 3.12. Qt, Windows Shell, настольный профиль и брендинг
в пакет не входят.

```python
from pathlib import Path
from atom_search_core import SearchService

service = SearchService(Path("catalog.sqlite3"))
report = service.scan(r"C:\documents")
results = service.search("КМ101")
service.close()
```

Вызывающий клиент выбирает рабочую папку, расположение индекса, фоновое выполнение
и время закрытия. `scan(root, progress=callback, cancel=threading.Event())` передаёт
прогресс словарями. `search(query, limit=100, root=None)` возвращает `SearchResult`;
`status(root=None)` возвращает состояние каталога. Содержимое документов не читается.

Публичные `Source`, `Embedder`, `Cancellation` и `ProgressCallback` описывают границы
интеграции. В конструктор можно передать `source=` и `embedder=`. `Source.snapshots`
возвращает `DirectorySnapshot` с `FileMetadata`, не открывая интерфейс пользователя.

Для смыслового поиска отдельно устанавливаются зависимости extra `semantic`;
проверенные файлы модели передаются через `model_dir=`. Пакет не скачивает модель
или зависимости во время работы. Если модель недоступна, остаётся лексический поиск.
При пользовательском `embedder=` нужны смысловые зависимости для матричных операций.

Автономная сборка использует уже установленный setuptools, без build isolation:

```powershell
python -c "from setuptools.build_meta import build_wheel; build_wheel('C:/output/core-wheel')"
python -m pip install --no-index --no-deps C:/output/core-wheel/ssearch_core-0.2.0-py3-none-any.whl
```

Команду сборки выполняют из каталога этого проекта. `--no-deps` требует заранее
подготовить смысловые зависимости, если они нужны клиенту. Wheel базового пакета
можно установить отдельно; распространение регулируется `LICENSE.txt`.

MIT License. Автор: Филипп Белобородов (empty-complete), filipp.beloborodoff@yandex.ru.
Импорт `atom_search_core` сохранён для совместимости; имя distribution — `ssearch-core`.
