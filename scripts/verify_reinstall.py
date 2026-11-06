"""Verify the private, ignored F-drive reinstall bundle without starting services."""
import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1] / '.local' / 'reinstall'
    manifest = json.loads((root / 'bundle-manifest.json').read_text(encoding='utf8'))
    total = 0
    for relative, expected in manifest['files'].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise SystemExit('Missing or redirected recovery file: ' + relative)
        if path.stat().st_size != expected['bytes']:
            raise SystemExit('Recovery size mismatch: ' + relative)
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected['sha256']:
            raise SystemExit('Recovery hash mismatch: ' + relative)
        total += expected['bytes']
    for relative in manifest['directories']:
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_dir():
            raise SystemExit('Missing recovery directory: ' + relative)
    print(json.dumps({'verified_files': len(manifest['files']), 'verified_bytes': total,
                      'directories': len(manifest['directories']),
                      'services_started': False, 'model_calls': False}))


if __name__ == '__main__':
    main()
