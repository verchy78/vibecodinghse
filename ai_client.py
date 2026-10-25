"""ai_client.py - логика main.py для использования из окна (app.py) + работа с файлами.

app.py вызывает ai_client.ask(текст) и получает ответ строкой.

Модель сама выбирает действие: OPEN (сайт), APP (программа), SEARCH (поиск), OPENFILE, READ, LIST,
MKDIR, SAVE, RENAME, MOVE, DELETE или NO (просто ответить).

Команды (работают без участия модели):
    /web запрос            принудительный поиск
    /open адрес            открыть сайт в Chrome
    /app название          запустить приложение (Telegram, Блокнот, Word…)
    /file путь             открыть файл или папку
    /read путь             прочитать файл и кратко пересказать
    /ls [путь]             показать содержимое папки
    /mkdir путь            создать папку
    /save [имя]            сохранить последний ответ в файл
    /rename путь -> имя    переименовать
    /move путь -> папка    переместить в папку
    /rm путь               удалить (в корзину, после подтверждения)
    /undo                  отменить последнее переименование/перемещение/удаление

Безопасность:
  - менять, переименовывать, перемещать и удалять можно только внутри папок
    «Документы», «Рабочий стол», «Загрузки» (сами эти папки и домашнюю не трогаем);
  - удаление всегда с подтверждением («да») и только в корзину, не навсегда;
  - существующие файлы не перезаписываются без подтверждения;
  - программы и скрипты (.exe, .bat, .ps1 и т.п.) как файлы не открываются и не создаются;
  - приложения запускаются только по названию и без параметров; командная строка, PowerShell
    и редактор реестра не запускаются (список BLOCKED_APP_WORDS можно менять).
"""
from __future__ import annotations

import datetime
import difflib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus, urlparse

try:
    import trafilatura
except ImportError:
    trafilatura = None

REPO_ID = "unsloth/Qwen3.5-4B-GGUF"
FILE_PATTERN = "*UD-Q4_K_XL.gguf"
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
LOCAL_PATH = BASE_DIR / "models" / "Qwen3.5-4B-UD-Q4_K_XL.gguf"

MAX_RESULTS = 4
PAGES_TO_READ = 2
PAGE_CHARS = 2500

MAX_FILE_CHARS = 8000   # сколько символов файла отдаём модели (контекст ограничен)
MAX_LIST = 60           # сколько элементов папки показывать

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


# ---------- модель ----------
_llm = None
_load_lock = threading.Lock()   # модель грузится один раз
_ask_lock = threading.Lock()    # запросы выполняются по одному
history: list[dict] = []
_last_answer: str | None = None   # последний «обычный» ответ модели (для сохранения в файл)
_pending: dict | None = None      # действие, ожидающее подтверждения («да»/«нет»)
_undo: list[tuple[Path, Path]] = []   # (откуда, куда) для /undo
_opened = False                   # была ли открыта внешняя программа/страница
_apps_cache: dict = {"items": [], "time": 0.0}   # список установленных приложений
_apps_lock = threading.Lock()


def _get_llm():
    global _llm
    with _load_lock:
        if _llm is None:
            from llama_cpp import Llama
            if os.path.exists(LOCAL_PATH):
                _llm = Llama(model_path=str(LOCAL_PATH), n_ctx=8192, verbose=False)
            else:
                _llm = Llama.from_pretrained(
                    repo_id=REPO_ID, filename=FILE_PATTERN, n_ctx=8192, verbose=False
                )
    return _llm


def warmup():
    """Загрузить модель заранее (можно вызвать в фоновом потоке при старте окна)."""
    threading.Thread(target=_list_apps, daemon=True).start()   # заодно соберём список приложений
    _get_llm()


def reset_history():
    global _last_answer, _pending
    history.clear()
    _last_answer = None
    _pending = None


def consume_opened():
    """True, если после прошлого вызова была открыта страница/файл/папка (флаг сбрасывается)."""
    global _opened
    result, _opened = _opened, False
    return result


def today():
    d = datetime.date.today()
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]} {d.year} года"


def strip_think(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    return re.sub(r"<think>.*", "", text, flags=re.S).strip()


# ---------- интернет и поиск ----------
def has_internet(timeout=1.5):
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=timeout).close()
        return True
    except OSError:
        return False


def web_search(query):
    """Возвращает текст результатов или None, если поиск не удался."""
    try:
        from ddgs import DDGS
        results = list(DDGS().text(query, max_results=MAX_RESULTS))
    except Exception:
        return None
    if not results:
        return None

    parts = []
    for i, r in enumerate(results):
        title, url, snippet = r.get("title", ""), r.get("href", ""), r.get("body", "")
        text = snippet
        if trafilatura and i < PAGES_TO_READ:
            try:
                html = trafilatura.fetch_url(url)
                page = trafilatura.extract(html) if html else None
                if page:
                    text = page[:PAGE_CHARS]
            except Exception:
                pass
        parts.append(f"[{i + 1}] {title}\n{url}\n{text}")
    return "\n\n".join(parts)


# ---------- открытие страницы в Chrome ----------
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_chrome():
    for p in CHROME_PATHS:
        if os.path.exists(p):
            return p
    return shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")


def normalize_target(target):
    """Приводит запрос к http(s)-адресу. Всё, что не похоже на адрес, ищется в Google."""
    target = target.strip().strip('"\'<>`')
    if re.match(r"^https?://", target, re.I):
        return target
    if re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", target):   # например github.com/foo
        return "https://" + target
    return "https://www.google.com/search?q=" + quote_plus(target)


def open_in_chrome(target):
    url = normalize_target(target)
    if urlparse(url).scheme not in ("http", "https"):   # защита от file://, javascript: и т.п.
        return None
    chrome = find_chrome()
    if chrome:
        subprocess.Popen([chrome, url])
    else:
        webbrowser.open(url)    # запасной вариант: браузер по умолчанию
    return url


def handle_open(target):
    global _opened
    opened = open_in_chrome(target)
    if opened:
        _opened = True
    return f"Открыл в Chrome: {opened}" if opened else "Не удалось открыть: адрес не http(s)."


# =====================================================================
#                          ФАЙЛЫ И ПАПКИ
# =====================================================================

def _std_dir(kind: str, fallback: str) -> Path:
    """Системная папка (Рабочий стол и т.п.) с учётом OneDrive и локализации."""
    try:
        from PySide6.QtCore import QStandardPaths
        path = QStandardPaths.writableLocation(getattr(QStandardPaths.StandardLocation, kind))
        if path:
            return Path(path)
    except Exception:
        pass
    return Path.home() / fallback


HOME = Path.home()
DESKTOP = _std_dir("DesktopLocation", "Desktop")
DOCUMENTS = _std_dir("DocumentsLocation", "Documents")
DOWNLOADS = _std_dir("DownloadLocation", "Downloads")
WORKSPACE = DOCUMENTS / "Qwen"      # сюда по умолчанию сохраняются новые файлы
TRASH = WORKSPACE / "_Корзина"      # запасная корзина, если не установлен send2trash

# Внутри этих папок разрешено создавать, переименовывать, перемещать и удалять.
WRITE_ROOTS = [DESKTOP, DOCUMENTS, DOWNLOADS]

# Программы и скрипты: не открываем.
BLOCKED_OPEN_EXT = {
    ".exe", ".bat", ".cmd", ".com", ".msi", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".scr", ".lnk", ".reg", ".hta", ".cpl", ".dll", ".jar", ".py", ".pyw",
    ".sh", ".app", ".command", ".appimage", ".apk", ".url", ".msc",
}
# Запускаемые файлы: не создаём и не даём имя с таким расширением.
BLOCKED_WRITE_EXT = {
    ".exe", ".bat", ".cmd", ".com", ".msi", ".ps1", ".psm1", ".vbs", ".vbe", ".scr", ".lnk",
    ".reg", ".hta", ".cpl", ".dll", ".jar", ".wsf", ".wsh", ".url", ".msc", ".command",
    ".app", ".apk",
}


def _aliases() -> dict[str, Path]:
    return {
        "рабочий стол": DESKTOP, "рабочем столе": DESKTOP,
        "рабочего стола": DESKTOP, "рабочему столу": DESKTOP,
        "мои документы": DOCUMENTS, "документы": DOCUMENTS,
        "документах": DOCUMENTS, "документов": DOCUMENTS,
        "загрузки": DOWNLOADS, "загрузках": DOWNLOADS, "загрузок": DOWNLOADS,
        "рабочая папка": WORKSPACE, "рабочей папке": WORKSPACE,
        "домашняя папка": HOME, "домашней папке": HOME, "домашнюю папку": HOME,
    }


def _fmt(path) -> str:
    return f"`{path}`"


def _clean(raw: str) -> str:
    return (raw or "").strip().strip("\"'«»`<>").strip()


def resolve_path(raw: str) -> Path:
    """Текст пользователя -> путь. Понимает «рабочий стол/файл.txt», ~, переменные среды.
    Относительные пути считаются от рабочей папки бота."""
    expanded = os.path.expandvars(os.path.expanduser(_clean(raw)))
    if Path(expanded).is_absolute():
        return Path(expanded)
    aliases = _aliases()
    for alias in sorted(aliases, key=len, reverse=True):
        pattern = re.compile(
            r"(?:^|(?<=[\s/\\]))(?:(?:на|в|во|из|с|со)\s+)?" + re.escape(alias) + r"(?=$|[\s/\\])",
            re.I,
        )
        m = pattern.search(expanded)
        if m:
            rest = (expanded[:m.start()] + " " + expanded[m.end():]).strip(" /\\")
            rest = rest.replace("\\", "/")
            return aliases[alias] / rest if rest else aliases[alias]
    return WORKSPACE / expanded.replace("\\", "/")


def _find_in_dir(parent: Path, name: str, want_dir: bool = False) -> Path | None:
    """Найти элемент без учёта регистра и (если однозначно) без расширения."""
    if not name:
        return None
    try:
        if not parent.is_dir():
            return None
        entries = [e for e in parent.iterdir() if not want_dir or e.is_dir()]
    except OSError:
        return None
    low = name.lower()
    exact = [e for e in entries if e.name.lower() == low]
    if len(exact) == 1:
        return exact[0]
    by_stem = [e for e in entries if e.stem.lower() == low]
    if len(by_stem) == 1:
        return by_stem[0]
    return None


def _existing(path: Path, want_dir: bool = False) -> Path | None:
    try:
        if path.exists() and (not want_dir or path.is_dir()):
            return path
    except OSError:
        return None
    return _find_in_dir(path.parent, path.name, want_dir)


def _locate(raw: str, want_dir: bool = False) -> Path | None:
    """Найти существующий файл/папку. Короткие имена ищутся в рабочей папке бота,
    на рабочем столе, в документах и загрузках."""
    s = _clean(raw)
    if not s:
        return None
    found = _existing(resolve_path(s), want_dir)
    if found:
        return found
    expanded = os.path.expandvars(os.path.expanduser(s))
    if not Path(expanded).is_absolute():
        rel = expanded.replace("\\", "/")
        for base in (WORKSPACE, DESKTOP, DOCUMENTS, DOWNLOADS):
            found = _existing(base / rel, want_dir)
            if found:
                return found
    return None


# ---------- проверки безопасности ----------
def _resolved(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p.absolute()


def _inside(p: Path, root: Path, allow_equal: bool = False) -> bool:
    rp, rr = _resolved(p), _resolved(root)
    if rp == rr:
        return allow_equal
    try:
        rp.relative_to(rr)
        return True
    except ValueError:
        return False


def _in_write_roots(p: Path, allow_root: bool = False) -> bool:
    return any(_inside(p, r, allow_equal=allow_root) for r in [WORKSPACE] + WRITE_ROOTS)


def _is_protected(p: Path) -> bool:
    rp = _resolved(p)
    protected = {_resolved(x) for x in (HOME, DESKTOP, DOCUMENTS, DOWNLOADS, WORKSPACE, TRASH)}
    return rp in protected or rp == rp.parent


def _roots_hint() -> str:
    return "«Документы», «Рабочий стол» и «Загрузки»"


def _modify_check(path: Path) -> str | None:
    """Можно ли переименовывать/перемещать/удалять этот элемент."""
    if _is_protected(path):
        return f"Эту папку я не трогаю: {_fmt(path)}."
    if not _in_write_roots(path):
        return f"Менять, переносить и удалять можно только внутри папок {_roots_hint()}."
    return None


def _delete_check(path: Path) -> str | None:
    if _inside(path, TRASH, allow_equal=True):
        return "Свою корзину я не очищаю: удалите файлы оттуда вручную или верните через /undo."
    return None


def _write_check(path: Path) -> str | None:
    if path.suffix.lower() in BLOCKED_WRITE_EXT:
        return "Программы и скрипты (.exe, .bat, .ps1 и т.п.) я не создаю."
    if _is_protected(path):
        return f"Сюда сохранить нельзя: {_fmt(path)}."
    if not _in_write_roots(path):
        return f"Сохранять можно только в папки {_roots_hint()}."
    if path.is_dir():
        return f"{_fmt(path)} — это папка, нужно имя файла."
    return None


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _human(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{int(value)} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} Б"


def _count_items(path: Path, cap: int = 5000) -> str:
    n = 0
    try:
        for _ in path.rglob("*"):
            n += 1
            if n >= cap:
                return f"{cap}+"
    except OSError:
        pass
    return str(n)


# ---------- открыть ----------
def handle_open_file(target: str) -> str:
    """Открыть файл (в программе по умолчанию) или папку (в проводнике)."""
    global _opened
    if not target.strip():
        return "Укажите, что открыть. Например: `/file документы/отчёт.docx`."
    path = _locate(target)
    if path is None:
        return f"Не нашёл {_fmt(_clean(target))}."
    if path.is_file() and path.suffix.lower() in BLOCKED_OPEN_EXT:
        return "Программы и скрипты я не запускаю и не открываю: это небезопасно."
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        return f"Не удалось открыть: {e}"
    _opened = True
    return f"Открыл: {_fmt(path)}"


# ---------- содержимое папки ----------
def _ensure_workspace() -> Path:
    try:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return WORKSPACE


def handle_list(target: str) -> str:
    if _clean(target) in ("", "."):
        path = _ensure_workspace()
    else:
        path = _locate(target)
    if path is None:
        return f"Не нашёл {_fmt(_clean(target))}."
    if path.is_file():
        return f"{_fmt(path)} — это файл, а не папка."
    try:
        entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as e:
        return f"Не удалось прочитать папку: {e}"
    if not entries:
        return f"Папка {_fmt(path)} пуста."
    lines = []
    for e in entries[:MAX_LIST]:
        if e.is_dir():
            lines.append(f"- 📁 `{e.name}`")
        else:
            try:
                lines.append(f"- 📄 `{e.name}` — {_human(e.stat().st_size)}")
            except OSError:
                lines.append(f"- 📄 `{e.name}`")
    text = f"Содержимое {_fmt(path)}:\n\n" + "\n".join(lines)
    if len(entries) > MAX_LIST:
        text += f"\n\n…и ещё {len(entries) - MAX_LIST}"
    return text


# ---------- создать папку ----------
def handle_mkdir(target: str) -> str:
    if not target.strip():
        return "Укажите имя папки. Например: `/mkdir документы/Отчёты`."
    path = resolve_path(target)
    if _is_protected(path) or not _in_write_roots(path):
        return f"Создавать папки можно только внутри {_roots_hint()}."
    if path.exists():
        return f"{_fmt(path)} уже существует."
    try:
        path.mkdir(parents=True)
    except OSError as e:
        return f"Не удалось создать папку: {e}"
    return f"Создал папку: {_fmt(path)}"


# ---------- прочитать файл ----------
def _read_text(path: Path) -> tuple[str | None, str | None]:
    """(текст, ошибка)."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return None, "Для чтения PDF нужен пакет pypdf: `pip install pypdf`."
            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:30])
        elif ext == ".docx":
            try:
                import docx
            except ImportError:
                return None, "Для чтения Word нужен пакет python-docx: `pip install python-docx`."
            text = "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
        else:
            with open(path, "rb") as f:
                data = f.read(400_000)
            if b"\x00" in data:
                return None, "Это бинарный файл, как текст его прочитать нельзя."
            text = None
            for enc in ("utf-8-sig", "cp1251"):
                try:
                    text = data.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                text = data.decode("utf-8", errors="replace")
    except Exception as e:
        return None, f"Не удалось прочитать файл: {e}"
    if not text.strip():
        return None, "В файле нет текста."
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + "\n[…файл обрезан…]"
    return text, None


def _load_for_prompt(target: str) -> tuple[Path | None, str | None, str | None]:
    """(путь, текст, готовый ответ-ошибка)."""
    path = _locate(target)
    if path is None:
        return None, None, f"Не нашёл {_fmt(_clean(target))}."
    if path.is_dir():
        return None, None, handle_list(target)
    text, err = _read_text(path)
    if err:
        return None, None, err
    return path, text, None


# ---------- сохранить файл ----------
def _strip_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```[\w+-]*\n(.*?)\n?```\s*$", text, re.S)
    return m.group(1) if m else text


def _generate_file_content(user_text: str) -> str:
    system = (
        "Ты пишешь содержимое файла по запросу пользователя. Выведи ТОЛЬКО само содержимое "
        "файла: без вступлений, пояснений и без обрамления тройными обратными кавычками. "
        "Язык - как в запросе."
    )
    msgs = [{"role": "system", "content": system}] + history[-5:-1] + [
        {"role": "user", "content": user_text + "\n\nВыведи только содержимое файла."}
    ]
    out = _get_llm().create_chat_completion(messages=msgs, max_tokens=2000)
    return _strip_fence(strip_think(out["choices"][0]["message"]["content"] or ""))


def _dialog_text() -> str:
    lines = []
    for m in history[:-1]:
        who = "Вы" if m["role"] == "user" else "Qwen"
        lines.append(f"{who}: {m['content']}")
    return "\n\n".join(lines)


def _content_for_save(user_text: str) -> str:
    low = user_text.lower()
    if re.search(r"диалог|переписк|чат|разговор", low):
        return _dialog_text()
    if _last_answer and re.search(r"ответ|это\b|выше|предыдущ|прошл|последн", low):
        return _last_answer
    return _generate_file_content(user_text)


def _write_file(path: Path, content: str) -> str:
    err = _write_check(path)
    if err:
        return err
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"Не удалось сохранить файл: {e}"
    return f"Сохранил: {_fmt(path)}"


def _prepare_save(target: str, content: str) -> str:
    global _pending
    target = _clean(target)
    if not target:
        target = "qwen_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".txt"
    path = resolve_path(target)
    if path.suffix == "":
        path = path.with_suffix(".txt")
    err = _write_check(path)
    if err:
        return err
    if not content.strip():
        return "Нечего сохранять: содержимое пустое."
    if path.exists():
        _pending = {"kind": "write", "path": path, "content": content}
        return f"Файл {_fmt(path)} уже существует. Перезаписать? Напишите «да» или «нет»."
    return _write_file(path, content)


def handle_save(user_text: str, target: str) -> str:
    return _prepare_save(target, _content_for_save(user_text))


# ---------- переименовать / переместить / удалить ----------
_ARROW = re.compile(r"\s*(?:-->|->|=>|→)\s*")
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _split_arrow(arg: str) -> tuple[str, str] | None:
    parts = _ARROW.split(arg.strip(), maxsplit=1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return parts[0].strip(), parts[1].strip()


def _push_undo(src: Path, dst: Path) -> None:
    _undo.append((src, dst))
    del _undo[:-20]


def handle_rename(arg: str) -> str:
    parts = _split_arrow(arg)
    if not parts:
        return "Формат: `/rename старое.txt -> новое.txt`."
    src_raw, new_name = parts
    src = _locate(src_raw)
    if src is None:
        return f"Не нашёл {_fmt(_clean(src_raw))}."
    err = _modify_check(src)
    if err:
        return err

    new_name = _clean(new_name)
    if re.search(r"[\\/]", new_name):
        return "При переименовании укажите только новое имя, без пути. Чтобы перенести, попросите «перемести»."
    if not new_name or new_name in (".", "..") or _BAD_NAME_CHARS.search(new_name):
        return "В новом имени есть недопустимые символы или оно пустое."
    if src.is_file() and not Path(new_name).suffix and src.suffix:
        new_name += src.suffix           # «переименуй a.txt в заметки» -> заметки.txt
    if Path(new_name).suffix.lower() in BLOCKED_WRITE_EXT:
        return "Давать файлам расширения программ и скриптов (.exe, .bat и т.п.) я не буду."

    dst = src.with_name(new_name)
    if dst == src:
        return "Имя не изменилось."
    if dst.exists() and not _same(dst, src):
        return f"Там уже есть {_fmt(dst.name)}. Выберите другое имя."
    try:
        src.rename(dst)
    except OSError as e:
        return f"Не удалось переименовать: {e}"
    _push_undo(src, dst)
    return f"Переименовал: {_fmt(src.name)} → {_fmt(dst.name)}"


def handle_move(arg: str) -> str:
    parts = _split_arrow(arg)
    if not parts:
        return "Формат: `/move файл.txt -> папка`."
    src_raw, dst_raw = parts
    src = _locate(src_raw)
    if src is None:
        return f"Не нашёл {_fmt(_clean(src_raw))}."
    err = _modify_check(src)
    if err:
        return err

    dst_dir = _locate(dst_raw, want_dir=True) or resolve_path(dst_raw)
    if dst_dir.exists() and not dst_dir.is_dir():
        return f"{_fmt(dst_dir)} — это файл, а не папка."
    if not _in_write_roots(dst_dir, allow_root=True):
        return f"Переносить можно только внутри папок {_roots_hint()}."
    if src.is_dir() and (_resolved(dst_dir) == _resolved(src) or _inside(dst_dir, src)):
        return "Нельзя перенести папку саму в себя."

    target = dst_dir / src.name
    if _same(target, src) if target.exists() else False:
        return "Файл уже лежит в этой папке."
    if target.exists():
        return f"В папке {_fmt(dst_dir)} уже есть {_fmt(src.name)}."
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
    except OSError as e:
        return f"Не удалось переместить: {e}"
    _push_undo(src, target)
    return f"Перенёс: {_fmt(src)} → {_fmt(target)}"


def _trash_note() -> str:
    try:
        import send2trash  # noqa: F401
        return "Он уйдёт в корзину, его можно будет вернуть оттуда."
    except ImportError:
        return f"Он переедет в {_fmt(TRASH)}, вернуть можно командой /undo."


def handle_delete(arg: str) -> str:
    """Первый шаг: проверить и спросить подтверждение. Само удаление - после «да»."""
    global _pending
    if not arg.strip():
        return "Укажите, что удалить. Например: `/rm загрузки/старый.zip`."
    path = _locate(arg)
    if path is None:
        return f"Не нашёл {_fmt(_clean(arg))}."
    err = _modify_check(path) or _delete_check(path)
    if err:
        return err
    if path.is_dir():
        what = f"папку {_fmt(path)} (внутри элементов: {_count_items(path)})"
    else:
        try:
            what = f"файл {_fmt(path)} ({_human(path.stat().st_size)})"
        except OSError:
            what = f"файл {_fmt(path)}"
    _pending = {"kind": "delete", "path": path}
    return f"Удалить {what}? {_trash_note()}\n\nНапишите «да» для подтверждения или «нет» для отмены."


def _trash(path: Path) -> str:
    err = _modify_check(path) or _delete_check(path)
    if err:
        return err
    if not path.exists():
        return f"{_fmt(path)} уже не существует."
    try:
        from send2trash import send2trash
    except ImportError:
        send2trash = None
    if send2trash:
        try:
            send2trash(str(path))
        except Exception as e:
            return f"Не удалось удалить: {e}"
        return f"Отправил в корзину: {_fmt(path)}"

    # запасной вариант: своя корзина (ничего не стирается навсегда)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = TRASH / f"{path.stem}_{stamp}{path.suffix}"
    try:
        TRASH.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dst))
    except OSError as e:
        return f"Не удалось удалить: {e}"
    _push_undo(path, dst)
    return f"Убрал в корзину: {_fmt(path)}. Вернуть: /undo."


def handle_undo() -> str:
    if not _undo:
        return "Отменять нечего."
    src, dst = _undo.pop()
    if not dst.exists():
        return f"Не могу вернуть: {_fmt(dst)} уже не существует."
    if src.exists():
        _undo.append((src, dst))
        return f"Не могу вернуть: на месте {_fmt(src)} уже что-то лежит."
    try:
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(src))
    except OSError as e:
        _undo.append((src, dst))
        return f"Не удалось вернуть: {e}"
    return f"Вернул на место: {_fmt(src)}"


# =====================================================================
#                             ПРИЛОЖЕНИЯ
# =====================================================================

# Системные утилиты, которые бот не запускает (список можно менять).
BLOCKED_APP_WORDS = [
    "regedit", "редактор реестра", "powershell", "pwsh", "cmd", "командная строка",
    "command prompt", "windows terminal", "терминал", "wscript", "cscript", "mshta",
]

# Как люди называют популярные программы -> как они называются в меню «Пуск».
BRAND_ALIASES = {
    "телеграм": "telegram", "телеграмм": "telegram", "телега": "telegram",
    "дискорд": "discord", "стим": "steam", "хром": "chrome", "гугл хром": "chrome",
    "файрфокс": "firefox", "фаерфокс": "firefox", "эдж": "edge", "опера": "opera",
    "яндекс браузер": "yandex", "ворд": "word", "эксель": "excel",
    "пауэрпоинт": "powerpoint", "повер поинт": "powerpoint", "аутлук": "outlook",
    "спотифай": "spotify", "зум": "zoom", "скайп": "skype", "вайбер": "viber",
    "ватсап": "whatsapp", "вотсап": "whatsapp", "обс": "obs", "тимс": "teams",
    "вс код": "visual studio code", "вскод": "visual studio code",
    "пайчарм": "pycharm", "блендер": "blender", "фотошоп": "photoshop",
    "эпик геймс": "epic games", "майнкрафт": "minecraft",
}

# Системные программы Windows: название -> (подпись, тип запуска, что запускать).
_WIN_BUILTIN: dict[str, tuple[str, str, str]] = {}


def _add_builtin(names, label, kind, target):
    for n in names:
        _WIN_BUILTIN[n] = (label, kind, target)


_add_builtin(["блокнот", "notepad"], "Блокнот", "cmd", "notepad.exe")
_add_builtin(["калькулятор", "calculator", "calc"], "Калькулятор", "cmd", "calc.exe")
_add_builtin(["paint", "пейнт", "паинт", "mspaint"], "Paint", "cmd", "mspaint.exe")
_add_builtin(["проводник", "explorer"], "Проводник", "cmd", "explorer.exe")
_add_builtin(["диспетчер задач", "task manager", "taskmgr"], "Диспетчер задач", "cmd", "taskmgr.exe")
_add_builtin(["ножницы", "snipping tool"], "Ножницы", "cmd", "snippingtool.exe")
_add_builtin(["панель управления", "control panel"], "Панель управления", "cmd", "control.exe")
_add_builtin(["параметры", "настройки", "settings"], "Параметры", "uri", "ms-settings:")

_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
     "r", "s", "t", "u", "f", "h", "c", "ch", "sh", "sh", "", "y", "", "e", "yu", "ya"],
))
_SKIP_APP = re.compile(r"uninstall|удалить|удаление|деинстал|readme|license|лиценз|документац|справк", re.I)


def _translit(text: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in text.lower())


def _norm_app(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s+#.]", " ", text.lower())).strip()


def _norm_query(name: str) -> str:
    q = _norm_app(_clean(name))
    q = re.sub(r"\b(?:приложение|приложуху|программу|программа|пожалуйста|app)\b", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def _query_forms(q: str) -> list[str]:
    forms = [q]
    if q in BRAND_ALIASES:
        forms.append(BRAND_ALIASES[q])
    tl = _translit(q)
    if tl != q:
        forms.append(tl)
    return forms


def _app_score(forms: list[str], name: str) -> float:
    n = _norm_app(name)
    best = 0.0
    for q in forms:
        if q == n:
            score = 1.0
        elif len(q) >= 3 and (n.startswith(q) or re.search(r"\b" + re.escape(q) + r"\b", n)):
            score = 0.92
        elif len(q) >= 3 and q in n:
            score = 0.85
        else:
            score = difflib.SequenceMatcher(None, q, n).ratio()
        best = max(best, score)
    return best


def _app_blocked(*texts: str) -> bool:
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in BLOCKED_APP_WORDS) + r")\b", re.I)
    return any(pattern.search(t or "") for t in texts)


def _list_apps_windows() -> list[tuple[str, str, str]]:
    items: list[tuple[str, str, str]] = []
    try:   # все приложения из меню «Пуск», включая магазинные
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = r.stdout.decode("utf-8-sig", errors="replace").strip()
        data = json.loads(raw) if raw else []
        if isinstance(data, dict):
            data = [data]
        for d in data:
            if d.get("Name") and d.get("AppID"):
                items.append((d["Name"], "appid", d["AppID"]))
    except Exception:
        pass
    if items:
        return items
    # запасной вариант: ярлыки из меню «Пуск»
    for env in ("ProgramData", "APPDATA"):
        root = Path(os.environ.get(env, "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        try:
            for lnk in root.rglob("*.lnk"):
                items.append((lnk.stem, "lnk", str(lnk)))
        except OSError:
            pass
    return items


def _list_apps_mac() -> list[tuple[str, str, str]]:
    items = []
    for base in (Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications"):
        try:
            items += [(p.stem, "macapp", str(p)) for p in base.glob("*.app")]
        except OSError:
            pass
    return items


def _list_apps_linux() -> list[tuple[str, str, str]]:
    items = []
    dirs = [
        Path("/usr/share/applications"), Path.home() / ".local/share/applications",
        Path("/var/lib/flatpak/exports/share/applications"),
        Path.home() / ".local/share/flatpak/exports/share/applications",
    ]
    for d in dirs:
        try:
            for f in d.glob("*.desktop"):
                name, hidden = None, False
                for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if line.startswith("Name=") and name is None:
                        name = line[5:].strip()
                    elif line in ("NoDisplay=true", "Hidden=true"):
                        hidden = True
                if name and not hidden:
                    items.append((name, "desktop", str(f)))
        except OSError:
            pass
    return items


def _list_apps() -> list[tuple[str, str, str]]:
    """Установленные приложения: (название, тип запуска, что запускать). Список кешируется."""
    with _apps_lock:
        if _apps_cache["items"] and time.time() - _apps_cache["time"] < 600:
            return _apps_cache["items"]
        if sys.platform == "win32":
            items = _list_apps_windows()
        elif sys.platform == "darwin":
            items = _list_apps_mac()
        else:
            items = _list_apps_linux()
        _apps_cache["items"], _apps_cache["time"] = items, time.time()
        return items


def _launch_item(item: tuple[str, str, str]) -> str:
    """Запустить приложение. Параметры не передаются, только само приложение."""
    global _opened
    name, kind, target = item
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        if kind == "appid":
            subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + target], **quiet)
        elif kind in ("lnk", "uri"):
            os.startfile(target)  # type: ignore[attr-defined]
        elif kind == "cmd":
            subprocess.Popen([target], **quiet)
        elif kind == "macapp":
            subprocess.Popen(["open", target], **quiet)
        elif kind == "desktop":
            if shutil.which("gio"):
                subprocess.Popen(["gio", "launch", target], **quiet)
            else:
                subprocess.Popen(["gtk-launch", Path(target).stem], **quiet)
        else:
            return f"Не знаю, как запустить «{name}»."
    except Exception as e:
        return f"Не удалось запустить «{name}»: {e}"
    _opened = True
    return f"Запустил: {name}"


def handle_open_app(name: str) -> str:
    """Запустить приложение по названию («телеграм», «блокнот», «Word»)."""
    global _pending
    q = _norm_query(name)
    if not q:
        return "Укажите название приложения. Например: `/app телеграм`."
    if _app_blocked(q):
        return "Эту системную утилиту я не открываю."
    if sys.platform == "win32" and q in _WIN_BUILTIN:
        return _launch_item(_WIN_BUILTIN[q])

    forms = _query_forms(q)
    ranked = sorted(
        ((_app_score(forms, it[0]), it) for it in _list_apps() if not _SKIP_APP.search(it[0])),
        key=lambda x: (-x[0], len(x[1][0])),
    )
    if not ranked or ranked[0][0] < 0.6:
        return f"Не нашёл приложение «{_clean(name)}» среди установленных."
    score, item = ranked[0]
    if _app_blocked(item[0], item[2]):
        return "Эту системную утилиту я не открываю."
    if score < 0.8:   # не уверены - уточняем, чтобы не запустить не то
        _pending = {"kind": "app", "item": item}
        return f"Не уверен: вы имели в виду «{item[0]}»? Напишите «да», чтобы запустить, или «нет»."
    return _launch_item(item)


# ---------- подтверждение ----------
_YES = {"да", "д", "yes", "y", "подтверждаю", "удали", "удалить", "перезапиши", "перезаписать"}
_NO = {"нет", "н", "no", "n", "отмена", "отмени", "не надо", "не нужно"}
_UNDO_RE = re.compile(
    r"^\s*(?:/undo|отмени(?:ть)?(?:\s+последнее(?:\s+действие)?)?|верни(?:\s+обратно|\s+как\s+было)?)\s*[.!]?\s*$",
    re.I,
)


def _run_pending(pending: dict) -> str:
    if pending["kind"] == "delete":
        return _trash(pending["path"])
    if pending["kind"] == "write":
        return _write_file(pending["path"], pending["content"])
    if pending["kind"] == "app":
        return _launch_item(pending["item"])
    return "Отменено."


# ---------- решение: что делать с сообщением ----------
def decide_action(history):
    system = (
        f"Сегодня {today()}. Выбери действие для последнего сообщения пользователя. "
        "Ответь ровно одной строкой в одном из форматов:\n"
        "OPEN: <адрес сайта или название сайта> — только если пользователь явно просит "
        "открыть сайт или страницу в интернете (в браузере);\n"
        "SEARCH: <короткий поисковый запрос> — если нужна актуальная информация из интернета "
        "(новости, цены, погода, свежие события, версии ПО, факты, в которых ты не уверен);\n"
        "APP: <название программы> — запустить установленную программу или приложение "
        "(Telegram, Блокнот, Word, Steam и т.п.);\n"
        "OPENFILE: <путь> — открыть файл или папку на компьютере;\n"
        "READ: <путь к файлу> — прочитать файл, чтобы ответить по его содержимому "
        "(пересказать, найти что-то, объяснить);\n"
        "LIST: <путь к папке> — показать, что лежит в папке;\n"
        "MKDIR: <путь> — создать папку;\n"
        "SAVE: <имя файла или путь> — сохранить текст, ответ или диалог в файл;\n"
        "RENAME: <путь> -> <новое имя> — переименовать файл или папку;\n"
        "MOVE: <путь> -> <папка назначения> — переместить файл или папку;\n"
        "DELETE: <путь> — удалить файл или папку;\n"
        "NO — во всех остальных случаях.\n"
        "Сайты — это OPEN, программы — APP, а файлы и папки на компьютере — OPENFILE. "
        "Пути пиши так, как их назвал "
        "пользователь (можно «рабочий стол», «документы», «загрузки»).\n"
        "Примеры:\n"
        "«открой youtube» -> OPEN: youtube\n"
        "«что нового в мире» -> SEARCH: новости сегодня\n"
        "«запусти телеграм» -> APP: телеграм\n"
        "«открой блокнот» -> APP: блокнот\n"
        "«открой папку загрузки» -> OPENFILE: загрузки\n"
        "«прочитай файл отчёт.txt» -> READ: отчёт.txt\n"
        "«что лежит на рабочем столе» -> LIST: рабочий стол\n"
        "«сохрани это в заметки.txt» -> SAVE: заметки.txt\n"
        "«переименуй a.txt в b.txt» -> RENAME: a.txt -> b.txt\n"
        "«перемести отчёт.docx в папку документы» -> MOVE: отчёт.docx -> документы\n"
        "«удали файл старый.zip из загрузок» -> DELETE: загрузки/старый.zip\n"
        "«привет» -> NO"
    )
    msgs = [{"role": "system", "content": system}] + history[-4:]
    out = _get_llm().create_chat_completion(messages=msgs, max_tokens=300, temperature=0.0)
    text = strip_think(out["choices"][0]["message"]["content"])
    m = re.search(
        r"\b(OPENFILE|OPEN|APP|SEARCH|READ|LIST|MKDIR|SAVE|RENAME|MOVE|DELETE)\s*:\s*(.+)", text
    )
    if not m:
        return None, None
    return m.group(1), m.group(2).strip().strip('"')


# ---------- диалог ----------
def _system_prompt():
    return (
        f"Ты полезный ассистент для 105-ти летней бабушки. Сегодня {today()}. "
        "Отвечай на языке пользователя. "
        "Если дан блок с результатами поиска, опирайся на него и указывай ссылки на источники. "
        "Если дан блок с содержимым файла, отвечай по нему. "
        "Дату и день недели бери только из этой строки, не вычисляй сам."
    )


def ask(prompt):
    """Отправить сообщение и вернуть ответ строкой. Вызывается из app.py."""
    user_text = (prompt or "").strip()
    if not user_text:
        return ""
    with _ask_lock:
        return _ask(user_text)


_USAGE = {
    "app": "Укажите название приложения. Например: `/app телеграм`.",
    "file": "Укажите, что открыть. Например: `/file документы/отчёт.docx`.",
    "mkdir": "Укажите имя папки. Например: `/mkdir документы/Отчёты`.",
    "rename": "Формат: `/rename старое.txt -> новое.txt`.",
    "move": "Формат: `/move файл.txt -> папка`.",
    "rm": "Укажите, что удалить. Например: `/rm загрузки/старый.zip`.",
}


def _file_command(cmd: str, arg: str) -> str:
    cmd = {"mv": "move", "delete": "rm", "del": "rm"}.get(cmd, cmd)
    if cmd in _USAGE and not arg:
        return _USAGE[cmd]
    if cmd == "app":
        return handle_open_app(arg)
    if cmd == "file":
        return handle_open_file(arg)
    if cmd == "ls":
        return handle_list(arg)
    if cmd == "mkdir":
        return handle_mkdir(arg)
    if cmd == "rename":
        return handle_rename(arg)
    if cmd == "move":
        return handle_move(arg)
    if cmd == "rm":
        return handle_delete(arg)
    if cmd == "undo":
        return handle_undo()
    if cmd == "save":
        if not _last_answer:
            return "Пока нечего сохранять: я ещё ничего не отвечал."
        return _prepare_save(arg, _last_answer)
    return "Неизвестная команда."


def _finish(reply: str) -> str:
    history.append({"role": "assistant", "content": reply})
    return reply


def _ask(user_text):
    global _pending, _last_answer

    # ответ на вопрос «Удалить? / Перезаписать?»
    if _pending is not None:
        pending, _pending = _pending, None
        answer = user_text.lower().strip(" .!")
        if answer in _YES:
            return _run_pending(pending)
        if answer in _NO:
            return "Отменено."
        # любое другое сообщение - обычный запрос, подтверждение сброшено

    # «отмени» / /undo
    if _UNDO_RE.match(user_text) and (user_text.lower().startswith("/undo") or _undo):
        return handle_undo()

    # команда /open - без участия модели
    if user_text.lower().startswith("/open "):
        return handle_open(user_text[6:])

    # файловые команды - тоже без участия модели
    m = re.match(
        r"^/(app|file|ls|mkdir|save|rename|move|mv|rm|delete|del|undo)(?:\s+(.*))?$",
        user_text, re.I | re.S,
    )
    if m:
        return _file_command(m.group(1).lower(), (m.group(2) or "").strip())

    forced = user_text.lower().startswith("/web ")
    forced_read = re.match(r"^/read\s+(.+)$", user_text, re.I | re.S)
    read_target = ""
    if forced:
        user_text = user_text[5:].strip()
    elif forced_read:
        read_target = forced_read.group(1).strip()
        user_text = "Кратко перескажи, что написано в этом файле."
    history.append({"role": "user", "content": user_text})

    if forced:
        action, arg = "SEARCH", user_text
    elif forced_read:
        action, arg = "READ", read_target
    else:
        action, arg = decide_action(history)

    # действия без ответа модели
    if action == "OPEN":
        return _finish(handle_open(arg))
    simple = {
        "APP": handle_open_app, "OPENFILE": handle_open_file, "LIST": handle_list,
        "MKDIR": handle_mkdir,
        "RENAME": handle_rename, "MOVE": handle_move, "DELETE": handle_delete,
    }
    if action in simple:
        return _finish(simple[action](arg))
    if action == "SAVE":
        return _finish(handle_save(user_text, arg))

    # файл или поиск - добавляем найденное в запрос к модели
    prompt_user = user_text
    if action == "READ":
        path, text, error = _load_for_prompt(arg)
        if error:
            return _finish(error)
        prompt_user = (
            f"Содержимое файла «{path.name}»:\n\n{text}\n\n"
            f"Вопрос пользователя: {user_text}"
        )
    elif action == "SEARCH" and has_internet():
        results = web_search(arg)
        if results:
            prompt_user = (
                f"Результаты поиска по запросу «{arg}»:\n\n{results}\n\n"
                f"Вопрос пользователя: {user_text}"
            )

    messages = [{"role": "system", "content": _system_prompt()}] + history[:-1] + [
        {"role": "user", "content": prompt_user}
    ]

    out = _get_llm().create_chat_completion(messages=messages, max_tokens=1024)
    answer = out["choices"][0]["message"]["content"] or ""

    history.append({"role": "assistant", "content": answer})
    _last_answer = answer
    return answer
