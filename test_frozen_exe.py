"""Opt-in real frozen EXE smoke test (network access, up to 180 seconds/task).
Run only after building: python test_frozen_exe.py
Uses a clean directory containing only the EXE; never imports the GUI/engine.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
from PIL import Image

ROOT = Path(__file__).resolve().parent


def main():
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    base = ROOT / 'build' / 'exe-smoke'
    base.mkdir(parents=True, exist_ok=True)
    exe = base / 'CardDownloader.exe'
    shutil.copy2(ROOT / 'dist' / 'CardDownloader.exe', exe)
    deck = base / 'sample.ydk'
    deck.write_text('#created by smoke test\n#main\n89631139\n89631139\n#extra\n!side\n', encoding='utf-8')
    environment = dict(os.environ)
    # A Python installation must not be needed by the frozen child.
    environment.pop('PYTHONHOME', None)
    environment.pop('PYTHONPATH', None)
    environment['PYTHONIOENCODING'] = 'utf-8'
    environment['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    out = base / 'piped-result'
    args = [str(exe), '--engine', str(deck), '--game', 'ygo', '--out', str(out),
            '--pics', str(base / 'pics'), '--workers', '1', '--download-workers', '1', '--force']
    result = subprocess.run(args, cwd=base, env=environment, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace', timeout=180)
    (base / 'piped-output.txt').write_text(result.stdout, encoding='utf-8')
    print(result.stdout)
    assert result.returncode == 0, ('piped rc', result.returncode)
    manifest = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['summary'] == {'processed': 1, 'ok': 1, 'failed': 0}, manifest
    card = manifest['cards'][0]
    assert card['qty'] == 2
    for record in card['files']:
        with Image.open(record['file']) as image:
            image.load()
            assert image.format == 'PNG'
            print('Frozen downloaded PNG:', image.size, card.get('lang'), card.get('source'))
    assert (out / 'cards.pdf').read_bytes().startswith(b'%PDF-')
    assert '排版卡册' in result.stdout, 'PDF progress pipe output missing'
    quiet_out = base / 'quiet-result'
    quiet_args = args.copy()
    quiet_args.remove('--force')
    quiet_args[quiet_args.index('--out') + 1] = str(quiet_out)
    # No console and no standard handles: exercise devnull fallback, cache and PDF.
    child = subprocess.Popen(quiet_args, cwd=base, env=environment,
                             creationflags=subprocess.DETACHED_PROCESS)
    assert child.wait(timeout=60) == 0, 'detached engine failed'
    assert (quiet_out / 'cards.pdf').read_bytes().startswith(b'%PDF-')
    print('Frozen EXE piped download + PNG + PDF and detached cached PDF passed.')


if __name__ == '__main__':
    main()
