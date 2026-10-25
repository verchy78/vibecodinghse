"""Создаёт ярлык приложения на рабочем столе (Windows и macOS).

Windows:
    python create_shortcut.py                       # ярлык -> pythonw.exe app.py (без консоли)
    python create_shortcut.py --exe dist\\MyApp.exe  # ярлык -> готовый .exe
macOS:
    python3 create_shortcut.py                      # на рабочем столе появится «Моё приложение.app»
    python3 create_shortcut.py --exe dist/MyApp.app # ярлык (алиас) на готовое .app
Общее:
    --dry-run   только показать, что будет создано
"""
from __future__ import annotations

import argparse
import base64
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "Моё приложение"          # имя ярлыка; то же, что в app.py
HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- Windows

def ps_quote(text: str) -> str:
    """Строка в одинарных кавычках PowerShell (внутри кавычки удваиваются)."""
    return "'" + text.replace("'", "''") + "'"


def build_script(name: str, target: str, arguments: str, workdir: str, icon: str) -> str:
    return f"""
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop {ps_quote(name + '.lnk')}
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = {ps_quote(target)}
$s.Arguments = {ps_quote(arguments)}
$s.WorkingDirectory = {ps_quote(workdir)}
$s.IconLocation = {ps_quote(icon)}
$s.Description = {ps_quote(name)}
$s.Save()
"""


def windows(args: argparse.Namespace) -> int:
    icon = HERE / "icon.ico"
    if args.exe:
        target = Path(args.exe).resolve()
        arguments = ""
    else:
        # pythonw.exe запускает Python без чёрного окна консоли.
        target = Path(sys.executable).with_name("pythonw.exe")
        arguments = f'"{HERE / "app.py"}"'

    script = build_script(args.name, str(target), arguments, str(HERE), str(icon))

    if args.dry_run:
        print(script)
        return 0
    if sys.platform != "win32":
        print("Ярлык .lnk создаётся только в Windows.")
        return 1
    if not target.exists():
        print(f"Не найден файл запуска: {target}")
        return 1

    # EncodedCommand: так не мешают пробелы, кавычки и русские буквы в путях.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        capture_output=True,
    )
    if result.returncode != 0:
        print("Не удалось создать ярлык:")
        print(result.stderr.decode("utf-8", errors="replace") or result.stderr.decode("cp866", errors="replace"))
        return 1
    print(f"Готово! Ярлык «{args.name}» создан на рабочем столе.")
    return 0


# ------------------------------------------------------------------ macOS

def mac_desktop() -> Path:
    return Path.home() / "Desktop"


def build_launcher_app(dest: Path, name: str, python: Path, script: Path, icns: Path) -> None:
    """Крошечный .app: внутри скрипт, который запускает app.py через Python окружения."""
    if dest.exists():
        shutil.rmtree(dest)
    macos = dest / "Contents" / "MacOS"
    res = dest / "Contents" / "Resources"
    macos.mkdir(parents=True)
    res.mkdir(parents=True)

    launcher = macos / "launcher"
    launcher.write_text(
        "#!/bin/bash\n"
        f'cd "{script.parent}"\n'
        f'exec "{python}" "{script}"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    if icns.exists():
        shutil.copy(icns, res / "icon.icns")

    plist = {
        "CFBundleName": name,
        "CFBundleDisplayName": name,
        "CFBundleIdentifier": "my.desktop.app",
        "CFBundleExecutable": "launcher",
        "CFBundleIconFile": "icon",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "NSHighResolutionCapable": True,
    }
    with open(dest / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(plist, f)


def make_alias(target: Path, desktop: Path, name: str) -> None:
    """Алиас Finder на готовое .app; если не вышло (нет прав) - обычная символьная ссылка."""
    link = desktop / f"{name}.app"
    if link.is_symlink() or link.exists():
        link.unlink() if link.is_symlink() else shutil.rmtree(link, ignore_errors=True)
    osa = (
        'tell application "Finder" to make new alias file to POSIX file '
        f'"{target}" at POSIX file "{desktop}"'
    )
    r = subprocess.run(["osascript", "-e", osa], capture_output=True, text=True)
    if r.returncode != 0:
        os.symlink(target, link)


def macos(args: argparse.Namespace) -> int:
    desktop = mac_desktop()
    icns = HERE / "icon.icns"
    if args.exe:
        target = Path(args.exe).resolve()
        what = f"алиас на {target}"
    else:
        target = desktop / f"{args.name}.app"
        what = f"{target} (запускает {HERE / 'app.py'} через {sys.executable})"

    if args.dry_run:
        print(f"Будет создан: {what}")
        return 0
    if sys.platform != "darwin":
        print("Этот режим работает только на macOS.")
        return 1
    if args.exe:
        if not target.exists():
            print(f"Не найдено: {target}")
            return 1
        make_alias(target, desktop, args.name)
    else:
        build_launcher_app(target, args.name, Path(sys.executable), HERE / "app.py", icns)
        # Сбросить кэш иконок Finder, чтобы новая иконка появилась сразу.
        subprocess.run(["touch", str(target)])
    print(f"Готово! «{args.name}» лежит на рабочем столе.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Создать ярлык на рабочем столе")
    parser.add_argument("--exe", help="путь к готовому .exe / .app (вместо запуска app.py через Python)")
    parser.add_argument("--name", default=APP_NAME, help="имя ярлыка")
    parser.add_argument("--dry-run", action="store_true", help="ничего не создавать, показать, что будет")
    args = parser.parse_args()
    if sys.platform == "darwin":
        return macos(args)
    return windows(args)


if __name__ == "__main__":
    sys.exit(main())
