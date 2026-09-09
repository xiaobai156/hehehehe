"""One-shot, hash-checked source transfer; removed before main is updated."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import subprocess


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    files = sorted(Path('.repair').glob('payload-*.txt'))
    if len(files) != 6:
        raise RuntimeError('Expected all six payloads')
    expected_payloads = [
        'ad0670f8cec4523f840776bcd93f6d7252e33582bf73c93babba7d2003e45102',
        '61827bec4ec6edc4aea66925c1dd2b925332197f997951a6cc6cadedcee66e84',
        '0bda122ffe48423269e069c6f45d6880e534870f3a6da0145b3eacb3bb342043',
        'd9c005d99f226fa80e3c0ac43274eb1a281e336d72fb3e3248ba93ce9f2f3710',
        '69387c7c65f45c31ccdc0fc228a402633a0a70596b75fd6290ab1992b5ec7b22',
        '88f00a5d901b8d9b1aa6d5a4c14e5999bc750ded86cfb0df44552340b29c51e2',
    ]
    frozen = {name: digest(Path(name).read_bytes()) for name in
              ['sites.json', 'outputs/recent_10_cache.json']}
    staged = {}
    for file, expected in zip(files, expected_payloads):
        raw = file.read_bytes()
        if digest(raw) != expected:
            raise RuntimeError(f'Payload hash mismatch: {file}')
        lines = raw.decode('utf-8').splitlines(keepends=True)
        index = 0
        while index < len(lines):
            if not lines[index].startswith('@file '):
                raise RuntimeError('Bad file header')
            name = lines[index][6:].rstrip('\n')
            path = PurePosixPath(name)
            if (path.is_absolute() or '..' in path.parts or name in frozen or
                not (name.startswith(('he_app/', 'tests/')) or name.endswith('.bat') or
                     name in {'.gitignore', '.gitattributes', 'REPAIR_NOTES.md', 'pyproject.toml'})):
                raise RuntimeError(f'Unapproved destination: {name}')
            if name in staged:
                raise RuntimeError(f'Duplicate destination: {name}')
            before = lines[index + 1].removeprefix('@before ').strip()
            after = lines[index + 2].removeprefix('@after ').strip()
            original = Path(name).read_bytes() if Path(name).exists() else b''
            if before == '-':
                if Path(name).exists():
                    raise RuntimeError(f'New file already exists: {name}')
            elif digest(original) != before:
                raise RuntimeError(f'Original source changed: {name}')
            original_lines = original.decode('utf-8').splitlines(keepends=True)
            edits = []
            index += 3
            last_stop = 0
            while lines[index] != '@end\n':
                label, start, stop, count = lines[index].split()
                start, stop, count = int(start), int(stop), int(count)
                if label != '@edit' or not last_stop <= start <= stop <= len(original_lines) or count < 0:
                    raise RuntimeError(f'Invalid edit offsets: {name}')
                edits.append((start, stop, lines[index + 1:index + 1 + count]))
                last_stop = stop
                index += 1 + count
            index += 1
            for start, stop, replacement in reversed(edits):
                original_lines[start:stop] = replacement
            data = ''.join(original_lines).encode('utf-8')
            if digest(data) != after:
                raise RuntimeError(f'Repaired source mismatch: {name}')
            staged[name] = data
    if len(staged) != 33:
        raise RuntimeError('Expected exactly 33 source/configuration/test changes')
    for name, data in staged.items():
        Path(name).parent.mkdir(parents=True, exist_ok=True)
        Path(name).write_bytes(data)
    Path('.repair/expected.json').write_text(json.dumps({name: digest(data) for name, data in staged.items()}), encoding='utf-8')
    Path('.repair/frozen.json').write_text(json.dumps(frozen), encoding='utf-8')
    subprocess.run(['git', 'add', '--', *staged], check=True)
    print('Applied and staged exactly 33 hash-verified changes; production data unchanged.')


if __name__ == '__main__':
    main()
