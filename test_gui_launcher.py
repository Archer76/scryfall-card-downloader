from pathlib import Path
import shutil
import subprocess
import tempfile
import time

with tempfile.TemporaryDirectory(prefix='GUI 启动测试 ', dir=Path(__file__).resolve().parent) as folder:
    base = Path(folder)
    shutil.copy2('启动GUI.cmd', base / '启动GUI.cmd')
    (base / 'scryfall_cards_gui.pyw').write_text("from pathlib import Path\nPath(__file__).with_name('launched.txt').write_text('OK',encoding='utf-8')\n", encoding='utf-8')
    result = subprocess.run(['cmd.exe', '/c', str(base / '启动GUI.cmd')], capture_output=True, timeout=15)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    deadline = time.monotonic() + 5
    while not (base / 'launched.txt').exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (base / 'launched.txt').read_text(encoding='utf-8') == 'OK'
    print('Double-click launcher passed with Chinese characters and spaces in the folder path.')
