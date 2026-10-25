#!/usr/bin/env python3
"""Metadata-only directory inventory. Python 3.9+, standard library only.

No source file is opened, read, hashed or copied. Directory enumeration and
stat calls supply names, sizes, timestamps and link attributes.
"""
import argparse
import csv
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import sys
import time


GROUPS = {
    'PDF': 'pdf',
    'текст': 'doc docx rtf odt txt md',
    'таблицы': 'xls xlsx xlsm xlsb ods csv tsv',
    'презентации': 'ppt pptx odp',
    'чертежи DWG/DXF': 'dwg dxf dwf dwfx',
    'КОМПАС': 'cdw frw m3d a3d spw kdw',
    'SolidWorks': 'sldprt sldasm slddrw',
    '3D/BIM': 'step stp iges igs x_t x_b sat stl 3mf obj ifc rvt rfa nwd nwc',
    'расчёты/код': 'm mat mph py ipynb sm xmcd mcdx',
    'изображения': 'jpg jpeg png tif tiff bmp gif heic webp svg',
    'архивы': 'zip rar 7z gz tar bz2 xz',
    'почта': 'msg eml pst ost',
    'видео/аудио': 'mp4 avi mov mkv wmv mp3 wav m4a',
    'служебные': 'bak tmp lck log dwl dwl2 db ini lnk',
}
CATEGORIES = {ext: category for category, extensions in GROUPS.items() for ext in extensions.split()}
FIELDS = ['relative_path', 'full_path', 'name', 'extension', 'category',
          'size_bytes', 'modified_utc', 'created_utc', 'depth', 'path_length', 'status']


def native(path):
    """Extended Windows paths for deep folders, with ordinary paths in reports."""
    value = str(path)
    if os.name != 'nt' or value.startswith('\\\\?\\'):
        return value
    if value.startswith('\\\\'):
        return '\\\\?\\UNC\\' + value[2:]
    return '\\\\?\\' + value


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds')


def visible(value):
    # Keep tree/error entries on one line even on filesystems allowing control characters.
    return str(value).replace('\\', '\\\\').replace('\t', '\\t').replace('\r', '\\r').replace('\n', '\\n')


def amount(size):
    for unit in ('Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ', 'ПиБ'):
        if abs(size) < 1024 or unit == 'ПиБ':
            return f'{size:.1f} {unit}'
        size /= 1024


def category(name):
    extension = Path(name).suffix.lstrip('.').lower() or '(нет)'
    return extension, 'служебные' if name.startswith('~$') else CATEGORIES.get(extension, 'прочее')


def inventory(root, out):
    """Write a new local report directory; return 0 for complete, 2 for partial."""
    root = Path(os.path.abspath(os.path.expanduser(str(root))))
    if not stat.S_ISDIR(os.stat(native(root)).st_mode):
        raise ValueError(f'Корень не является папкой: {root}')
    out = Path(out).expanduser().resolve()
    resolved_root = root.resolve()
    if out == resolved_root or resolved_root in out.parents:
        raise ValueError('Папка отчёта должна находиться вне сканируемой папки.')
    # Verify initial access before creating any report. No file content is read.
    with os.scandir(native(root)) as probe:
        next(probe, None)
    out.mkdir(parents=True, exist_ok=False)

    started = datetime.now(timezone.utc)
    clock = time.monotonic()
    counts = dict(files=0, directories=0, bytes=0, errors=0, skipped_links=0,
                  long_paths=0, max_depth=0, max_path_length=0)
    by_ext, by_cat, by_top, by_year = {}, {}, {}, {}
    biggest = []

    print(f'Обхожу: {root}\nОтчёт: {out}', flush=True)
    with (out / 'tree.txt').open('w', encoding='utf-8-sig', newline='') as tree, \
         (out / 'files.tsv').open('w', encoding='utf-8-sig', newline='') as table, \
         (out / 'errors.txt').open('w', encoding='utf-8-sig', newline='') as errors:
        writer = csv.writer(table, delimiter='\t', lineterminator='\n')
        writer.writerow(FIELDS)
        tree.write(f'{root}\nНачало (UTC): {started.isoformat(timespec="seconds")}\n')

        def problem(relative, operation, exc):
            counts['errors'] += 1
            errors.write(f'{visible(relative or ".")}\t{operation}\t{visible(exc)}\n')
            errors.flush()

        def children(directory, relative, prefix, depth):
            items = []
            try:
                with os.scandir(native(directory)) as entries:
                    for entry in entries:
                        try:
                            info = entry.stat(follow_symlinks=False)
                            items.append((entry.name, info, None))
                        except OSError as exc:
                            items.append((entry.name, None, str(exc)))
            except OSError as exc:
                problem(relative, 'обход папки', exc)
                tree.write(prefix + '[ошибка обхода: см. errors.txt]\n')
            items.sort(key=lambda item: (not (item[1] and stat.S_ISDIR(item[1].st_mode)),
                                         item[0].casefold(), item[0]))
            for index in range(len(items) - 1, -1, -1):
                name, info, error = items[index]
                last = index == len(items) - 1
                rel = f'{relative}/{name}' if relative else name
                stack.append((rel, name, info, error, prefix,
                              '└── ' if last else '├── ',
                              prefix + ('    ' if last else '│   '), depth))

        # Iterative depth-first walk avoids Python's recursion limit.
        stack = []
        children(root, '', '', 0)
        last_progress = time.monotonic()
        while stack:
            rel, name, info, error, prefix, branch, child_prefix, depth = stack.pop()
            full = root.joinpath(*rel.split('/'))
            is_dir = info is not None and stat.S_ISDIR(info.st_mode)
            # Reparse points include DFS namespace entries, which must be walked.
            # Only name-surrogate tags (bit 29: symlinks/junctions) redirect names.
            is_link = info is not None and (stat.S_ISLNK(info.st_mode) or
                bool(getattr(info, 'st_reparse_tag', 0) & 0x20000000))
            counts['max_depth'] = max(counts['max_depth'], depth + int(is_dir))
            counts['max_path_length'] = max(counts['max_path_length'], len(str(full)))
            counts['long_paths'] += int(len(str(full)) > 260)
            if is_link:
                counts['skipped_links'] += 1
            if is_dir:
                counts['directories'] += 1
                tree.write(f'{prefix}{branch}{visible(name)}/' +
                           ('  [ссылка: обход пропущен]' if is_link else '') + '\n')
                if not is_link:
                    children(full, rel, child_prefix, depth + 1)
            else:
                counts['files'] += 1
                ext, cat = category(name)
                size, modified, created = '', '', ''
                status = 'link_skipped' if is_link else 'ok'
                if error:
                    status = 'metadata_error'
                    problem(rel, 'метаданные', error)
                elif not is_link:
                    size = info.st_size
                    counts['bytes'] += size
                    try:
                        modified = timestamp(info.st_mtime)
                        # Unix ctime is NOT creation time. Leave this blank where unknown.
                        birth = getattr(info, 'st_birthtime', None)
                        if birth is None and os.name == 'nt':
                            birth = info.st_ctime
                        created = timestamp(birth) if birth is not None else ''
                    except (ValueError, OSError, OverflowError) as exc:
                        status = 'timestamp_error'
                        problem(rel, 'дата файла', exc)
                    for buckets, key in ((by_ext, ext), (by_cat, cat),
                                         (by_top, rel.split('/')[0] if '/' in rel else '(корень)'),
                                         (by_year, modified[:4] or '(неизвестно)')):
                        count, volume = buckets.get(key, (0, 0))
                        buckets[key] = count + 1, volume + size
                    biggest.append((size, rel))
                    if len(biggest) > 100:
                        biggest.sort(reverse=True)
                        del biggest[50:]
                note = (f'{size} Б; {modified}' if status == 'ok' else
                        '[ссылка: обход пропущен]' if is_link else '[ошибка: см. errors.txt]')
                tree.write(f'{prefix}{branch}{visible(name)}  [{note}]\n')
                writer.writerow([rel, str(full), name, ext, cat, size, modified, created,
                                 depth, len(str(full)), status])
            if time.monotonic() - last_progress >= 5:
                print(f'  Файлов: {counts["files"]:,}; папок: {counts["directories"]:,}; '
                      f'ошибок: {counts["errors"]}; {time.monotonic() - clock:.0f} с', flush=True)
                tree.flush()
                table.flush()
                last_progress = time.monotonic()

    status = ('complete_with_errors' if counts['errors'] else
              'complete_with_skipped_links' if counts['skipped_links'] else 'complete')
    with (out / 'summary.txt').open('w', encoding='utf-8-sig') as summary:
        summary.write(f'Опись: {root}\nНачало (UTC): {started.isoformat(timespec="seconds")}\n'
                      f'Окончание (UTC): {datetime.now(timezone.utc).isoformat(timespec="seconds")}\n'
                      f'Время обхода: {time.monotonic() - clock:.1f} с\nСтатус: {status}\n'
                      f'files={counts["files"]}; directories={counts["directories"]}; '
                      f'bytes={counts["bytes"]}; errors={counts["errors"]}\n'
                      f'Суммарный размер по метаданным: {amount(counts["bytes"])}\n'
                      f'Пропущено ссылок: {counts["skipped_links"]}\n'
                      f'Путей длиннее 260 символов: {counts["long_paths"]}\n'
                      f'Максимальная глубина: {counts["max_depth"]}\n'
                      f'Максимальная длина пути: {counts["max_path_length"]}\n')
        for title, buckets in (('Категории', by_cat), ('Расширения', by_ext),
                               ('Папки верхнего уровня', by_top), ('Год изменения (UTC)', by_year)):
            summary.write(f'\n{title}\nфайлов\tбайт\tгруппа\n')
            for key, (count, volume) in sorted(buckets.items(), key=lambda item: (-item[1][1], item[0])):
                summary.write(f'{count}\t{volume}\t{visible(key)}\n')
        summary.write('\n50 самых больших файлов\nбайт\tотносительный путь\n')
        for size, rel in sorted(biggest, reverse=True)[:50]:
            summary.write(f'{size}\t{visible(rel)}\n')
        summary.write('\nСкрытые, системные и временные файлы включены. Корень в число папок не входит.\n'
                      'Размеры ссылок и элементов с недоступными метаданными не суммируются.\n'
                      'Форматы определены по расширениям; содержание и дубли не проверялись.\n'
                      'Это опись во время обхода, а не согласованный снимок: файлы могут меняться.\n'
                      'Если есть ошибки, опись частичная; подробности в errors.txt.\n'
                      'Каталог файлов не подтверждает возможность извлечь факты из их содержимого.\n')
    completion = ('Обход завершён с ошибками' if counts['errors'] else
                  'Обход завершён; есть пропущенные ссылки' if counts['skipped_links'] else 'Готово')
    print(f'{completion}: '
          f'{counts["files"]:,} файлов, {counts["directories"]:,} папок.\n{out}', flush=True)
    return 2 if counts['errors'] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Текстовая опись папки без чтения/копирования файлов.')
    parser.add_argument('--root', '-Root', required=True, help='Явный путь к локальной или сетевой папке для обхода')
    parser.add_argument('--out', '-Out', type=Path, help='Новая папка отчёта вне корня обхода')
    args = parser.parse_args(argv)
    if not args.root.strip():
        parser.error('--root must be a non-empty explicit folder path')
    out = args.out or Path(__file__).resolve().parent / datetime.now().strftime('inventory_%Y%m%d_%H%M%S_%f')
    try:
        return inventory(args.root, out)
    except KeyboardInterrupt:
        print('\nОбход прерван. Если отчёт создан, он неполный.', file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        print(f'Не удалось завершить опись: {exc}\n'
              'Проверьте сеть/VPN, доступ к корню и новую локальную папку для отчёта.\n'
              'Если отчёт уже создан, он неполный.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
    sys.exit(main())
