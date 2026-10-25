"""Copy installed distribution license notices for the offline release."""
from importlib import metadata
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    destination = root / 'resources/licenses/dependencies'
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    for distribution in metadata.distributions():
        name = distribution.metadata['Name']
        for item in distribution.files or ():
            text = str(item).replace('\\', '/')
            basename = Path(text).name.casefold()
            if not any(term in basename for term in ('license', 'copying', 'notice')):
                continue
            source = distribution.locate_file(item)
            if not source.is_file() or source.suffix.casefold() not in ('.txt', '.md', ''):
                continue
            target = destination / name / text.replace('/', '__')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            copied += 1
    for source in (Path(sys.base_prefix) / 'LICENSE.txt', Path(sys.base_prefix) / 'LICENSE'):
        if source.is_file():
            (destination / 'CPython-LICENSE.txt').write_bytes(source.read_bytes())
            copied += 1
            break
    print(f'Copied {copied} installed license notices to {destination}')


if __name__ == '__main__':
    main()
