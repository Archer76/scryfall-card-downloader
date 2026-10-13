"""Build the embedded GUI as dist/CardDownloader.exe (Windows).

Install: python -m pip install -r requirements-build.txt
Inspect only: python build_exe.py --dry-run
Dependency/codec check only: python build_exe.py --check
Build after the engine and GUI embedding are ready: python build_exe.py

Only the generated GUI is an entry point. No engine/core Python files are
shipped as external data. PyInstaller's tkinter/_tkinter hooks collect the
Tcl/Tk native libraries, script trees, and runtime hook. PIL native libraries
and the required format plugins are explicitly collected as well.
"""
import argparse
import io
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PIL_IMPORTS = (
    'PIL.Image', 'PIL.features', 'PIL.WebPImagePlugin',
    'PIL.PngImagePlugin', 'PIL.JpegImagePlugin', 'PIL._webp', 'PIL._imaging',
)
TK_IMPORTS = ('tkinter', 'tkinter.ttk', '_tkinter')
EMBEDDED_MODULES = ('scryfall_cards_threaded', 'ptcg_engine', 'ygo_engine', 'gui_layer')


def build_commands(root=ROOT, executable=None):
    """Return ordered commands; all outputs are confined to this worktree."""
    root = Path(root).resolve()
    executable = executable or sys.executable
    command = [executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
               '--onefile', '--windowed', '--name', 'CardDownloader',
               '--distpath', str(root / 'dist'),
               '--workpath', str(root / 'build' / 'pyinstaller'),
               '--specpath', str(root / 'build' / 'spec'),
               '--collect-binaries', 'PIL']
    for module in PIL_IMPORTS + TK_IMPORTS:
        command.extend(('--hidden-import', module))
    for module in EMBEDDED_MODULES:
        command.extend(('--exclude-module', module))
    command.append(str(root / 'scryfall_cards_gui.pyw'))
    return ([executable, str(root / 'build_gui.py')], command)


def check_dependencies():
    """Prove actual lossless WebP decode -> PNG, not merely package presence."""
    import PIL
    from PIL import Image, features
    import PIL._webp
    import PyInstaller
    import tkinter
    import _tkinter

    if not features.check('webp'):
        raise RuntimeError('This Pillow installation has no WebP decoder.')
    source = Image.new('RGB', (2, 2), (23, 97, 181))
    webp = io.BytesIO()
    source.save(webp, format='WEBP', lossless=True)
    webp.seek(0)
    png = io.BytesIO()
    with Image.open(webp) as decoded:
        decoded.load()
        decoded.save(png, format='PNG')
    png.seek(0)
    with Image.open(png) as restored:
        restored.load()
        if restored.format != 'PNG' or restored.getpixel((0, 0)) != (23, 97, 181):
            raise RuntimeError('WebP to PNG round-trip failed.')
    tcl = tkinter.Tcl()
    library = Path(tcl.eval('info library'))
    if not (library / 'init.tcl').is_file():
        raise RuntimeError('Tcl script library is missing.')
    print(f'Python {sys.version.split()[0]}; Pillow {PIL.__version__}; '
          f'WebP {features.version("webp")}; PyInstaller {PyInstaller.__version__}; '
          f'Tcl/Tk {_tkinter.TCL_VERSION}/{_tkinter.TK_VERSION}')
    print('Lossless WebP decode -> PNG passed; Tcl init.tcl found.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Print commands without writing or building.')
    mode.add_argument('--check', action='store_true', help='Check dependencies/codecs without generating GUI or EXE.')
    args = parser.parse_args(argv)
    commands = build_commands()
    if args.dry_run:
        for command in commands:
            print(subprocess.list2cmdline(command) if sys.platform == 'win32' else shlex.join(command))
        return 0
    try:
        check_dependencies()
    except (ImportError, RuntimeError) as error:
        print(f'Build preflight failed: {error}\nInstall: {sys.executable} -m pip install -r requirements-build.txt', file=sys.stderr)
        return 1
    if args.check:
        return 0
    if sys.platform != 'win32':
        print('Build on Windows to produce CardDownloader.exe.', file=sys.stderr)
        return 1
    for command in commands:
        # Inherit stdin/stdout/stderr. Do not capture output through child pipes.
        result = subprocess.call(command, cwd=ROOT)
        if result:
            return result
    print(f'Created: {ROOT / "dist" / "CardDownloader.exe"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
