import os
import re
import socket
import shutil
import datetime
import subprocess
import webbrowser
from urllib.parse import quote_plus, urlparse

from llama_cpp import Llama
from ddgs import DDGS

try:
    import trafilatura
except ImportError:
    trafilatura = None

REPO_ID = "unsloth/Qwen3.5-4B-GGUF"
FILE_PATTERN = "*UD-Q4_K_XL.gguf"
LOCAL_PATH = r"models\Qwen3.5-4B-UD-Q4_K_XL.gguf"

MAX_RESULTS = 4
PAGES_TO_READ = 2
PAGE_CHARS = 2500
CONFIRM_OPEN = True     # спрашивать подтверждение перед открытием страницы

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


# ---------- модель ----------
print("Загрузка модели...")
if os.path.exists(LOCAL_PATH):
    llm = Llama(model_path=LOCAL_PATH, n_ctx=8192, verbose=False)
else:
    llm = Llama.from_pretrained(repo_id=REPO_ID, filename=FILE_PATTERN, n_ctx=8192, verbose=False)


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


# ---------- решение: поиск, открыть страницу или ничего ----------
def decide_action(history):
    system = (
        f"Сегодня {today()}. Выбери действие для последнего сообщения пользователя. "
        "Ответь ровно одной строкой в одном из форматов:\n"
        "OPEN: <адрес сайта или название сайта> — только если пользователь явно просит "
        "открыть сайт или страницу в браузере;\n"
        "SEARCH: <короткий поисковый запрос> — если нужна актуальная информация из интернета "
        "(новости, цены, погода, свежие события, версии ПО, факты, в которых ты не уверен);\n"
        "NO — во всех остальных случаях."
    )
    msgs = [{"role": "system", "content": system}] + history[-4:]
    out = llm.create_chat_completion(messages=msgs, max_tokens=300, temperature=0.0)
    text = strip_think(out["choices"][0]["message"]["content"])
    m = re.search(r"(OPEN|SEARCH):\s*(.+)", text)
    if not m:
        return None, None
    return m.group(1), m.group(2).strip().strip('"')


def handle_open(target):
    opened = open_in_chrome(target)
    return f"Открыл в Chrome: {opened}" if opened else "Не удалось открыть: адрес не http(s)."


# ---------- диалог ----------
SYSTEM = (
    # f"Ты полезный ассистент для 105-ти летней бабушки. Сегодня {today()}. Отвечай на языке пользователя. "
    "Если дан блок с результатами поиска, опирайся на него и указывай ссылки на источники. "
    "Дату и день недели бери только из этой строки, не вычисляй сам."
)
history = []

print("Готово. Введите сообщение (exit — выход).")
print("Команды: /web запрос — принудительный поиск, /open адрес — открыть в Chrome\n")

while True:
    user_text = input("Вы: ").strip()
    if user_text.lower() in {"exit", "quit", "выход"}:
        break
    if not user_text:
        continue

    # команда /open — без участия модели
    if user_text.lower().startswith("/open "):
        print(f"Qwen: {handle_open(user_text[6:])}\n")
        continue

    forced = user_text.lower().startswith("/web ")
    if forced:
        user_text = user_text[5:].strip()
    history.append({"role": "user", "content": user_text})

    if forced:
        action, arg = "SEARCH", user_text
    else:
        action, arg = decide_action(history)

    # модель решила открыть страницу
    if action == "OPEN":
        reply = handle_open(arg)
        print(f"Qwen: {reply}\n")
        history.append({"role": "assistant", "content": reply})
        continue

    # поиск (только если есть интернет)
    prompt_user = user_text
    if action == "SEARCH" and has_internet():
        results = web_search(arg)
        if results:
            print(f"[поиск: {arg}]")
            prompt_user = (
                f"Результаты поиска по запросу «{arg}»:\n\n{results}\n\n"
                f"Вопрос пользователя: {user_text}"
            )

    messages = [{"role": "system", "content": SYSTEM}] + history[:-1] + [
        {"role": "user", "content": prompt_user}
    ]

    print("Qwen: ", end="", flush=True)
    answer = ""
    for chunk in llm.create_chat_completion(messages=messages, max_tokens=1024, stream=True):
        delta = chunk["choices"][0]["delta"].get("content") or ""
        answer += delta
        print(delta, end="", flush=True)
    print("\n")

    history.append({"role": "assistant", "content": answer})