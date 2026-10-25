"""tts.py - озвучка ответов. Работает на компьютере, интернет не нужен.

Используются голоса, установленные в системе:
  1. Qt TextToSpeech (входит в PySide6) - основной вариант;
  2. если он недоступен: Windows - System.Speech через PowerShell,
     macOS - команда `say`, Linux - espeak-ng / espeak / spd-say.

Для русской озвучки в системе должен быть русский голос.
Windows: Параметры -> Время и язык -> Речь -> Добавить голоса.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading

from PySide6.QtCore import QLocale, QObject, QTimer, Signal

TTS_RATE = 0.1            # скорость речи: от -1.0 (медленно) до 1.0 (быстро)
TTS_VOLUME = 1.0          # громкость: от 0.0 до 1.0
MAX_SPOKEN_CHARS = 2500   # дальше длинные ответы не читаем (остановить можно кнопкой)

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\u200d\ufe0f\u25A0-\u25FF\u2B00-\u2BFF]"
)


def clean_for_speech(text: str) -> str:
    """Markdown-ответ -> текст, который приятно слушать (без символов разметки, ссылок и путей)."""
    t = text or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)                     # блоки кода пропускаем

    def inline_code(m: re.Match) -> str:                             # пути и длинный код не читаем
        s = m.group(1)
        return " " if (len(s) > 25 or "/" in s or "\\" in s) else s

    t = re.sub(r"`([^`]*)`", inline_code, t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)                 # [текст](ссылка) -> текст
    t = re.sub(r"https?://\S+", " ", t)                              # голые ссылки
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)              # заголовки
    t = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", t, flags=re.M)      # маркеры списков
    t = t.replace("→", ", ")
    t = re.sub(r"[*_~>|#]+", " ", t)                                 # остатки разметки
    t = _EMOJI.sub(" ", t)

    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]      # конец строки = пауза
    t = " ".join(ln if ln[-1] in ".!?…:;" else ln + "." for ln in lines)
    t = re.sub(r"\s+", " ", t).strip()

    if len(t) > MAX_SPOKEN_CHARS:
        cut = t[:MAX_SPOKEN_CHARS]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[: end + 1] if end > MAX_SPOKEN_CHARS // 2 else cut
    return t


def _language_of(text: str) -> str:
    cyr = len(re.findall(r"[а-яёА-ЯЁ]", text))
    lat = len(re.findall(r"[a-zA-Z]", text))
    return "ru" if cyr >= lat else "en"


_PS_SCRIPT = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "try { $s.SelectVoiceByHints('NotSet','NotSet',0,[Globalization.CultureInfo]'%LANG%') } catch {};"
    "$s.Rate = %RATE%;"
    "$s.Volume = %VOLUME%;"
    "[Console]::InputEncoding = [Text.Encoding]::UTF8;"
    "$t = [Console]::In.ReadToEnd();"
    "$s.Speak($t)"
)


class Speaker(QObject):
    """Озвучивает текст. speak() и stop() вызывайте из главного потока."""

    speaking_changed = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.backend = "none"
        self.has_russian_voice = True    # для запасных вариантов считаем, что голос есть
        self._speaking = False
        self._qt = None
        self._qt_cls = None
        self._ru_locale = None
        self._en_locale = None
        self._lang = ""
        self._proc: subprocess.Popen | None = None
        self._exe = ""

        self._poll = QTimer(self)
        self._poll.setInterval(300)
        self._poll.timeout.connect(self._check_process)

        if not self._init_qt():
            self._init_external()

    # --------------------------------------------------------- инициализация

    @property
    def available(self) -> bool:
        return self.backend != "none"

    def _init_qt(self) -> bool:
        try:
            from PySide6.QtTextToSpeech import QTextToSpeech
            if not QTextToSpeech.availableEngines():
                return False
            tts = QTextToSpeech(self)
            if tts.state() == QTextToSpeech.State.Error:
                return False
            locales = tts.availableLocales()
            self._ru_locale = next((l for l in locales if l.language() == QLocale.Language.Russian), None)
            self._en_locale = next((l for l in locales if l.language() == QLocale.Language.English), None)
            if not (self._ru_locale or self._en_locale or tts.availableVoices()):
                return False
            tts.setRate(TTS_RATE)
            tts.setVolume(TTS_VOLUME)
            tts.stateChanged.connect(self._on_qt_state)
        except Exception:
            return False
        self._qt, self._qt_cls = tts, QTextToSpeech
        self.has_russian_voice = self._ru_locale is not None
        self.backend = "qt"
        return True

    def _init_external(self) -> None:
        if sys.platform == "win32" and shutil.which("powershell"):
            self.backend, self._exe = "powershell", "powershell"
        elif sys.platform == "darwin" and shutil.which("say"):
            self.backend, self._exe = "say", "say"
        else:
            for exe in ("espeak-ng", "espeak", "spd-say"):
                if shutil.which(exe):
                    self.backend, self._exe = "linux", exe
                    break

    # ---------------------------------------------------------- Qt-вариант

    def _set_qt_language(self, lang: str) -> None:
        if lang == self._lang:
            return
        locale = self._ru_locale if lang == "ru" else self._en_locale
        if locale is None:
            return    # нужного языка нет - читаем текущим голосом
        self._qt.setLocale(locale)
        voices = self._qt.availableVoices()
        if voices:
            self._qt.setVoice(voices[0])
        self._lang = lang

    def _on_qt_state(self, state) -> None:
        cls = self._qt_cls
        speaking_states = {cls.State.Speaking}
        synthesizing = getattr(cls.State, "Synthesizing", None)
        if synthesizing is not None:
            speaking_states.add(synthesizing)
        self._set_speaking(state in speaking_states)

    # ---------------------------------------------------- запасные варианты

    def _external_command(self, text: str, lang: str) -> tuple[list[str], bytes | None]:
        data = text.encode("utf-8")
        if self.backend == "powershell":
            script = (
                _PS_SCRIPT.replace("%LANG%", "ru-RU" if lang == "ru" else "en-US")
                .replace("%RATE%", str(int(round(TTS_RATE * 10))))
                .replace("%VOLUME%", str(int(TTS_VOLUME * 100)))
            )
            return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], data
        if self.backend == "say":
            voice = "Milena" if lang == "ru" else "Samantha"
            return ["say", "-v", voice, "-r", str(int(175 + TTS_RATE * 80)), "-f", "-"], data
        if self._exe == "spd-say":
            return ["spd-say", "-l", "ru" if lang == "ru" else "en", "-w", text], None
        speed = str(int(160 + TTS_RATE * 60))
        return [self._exe, "-v", "ru" if lang == "ru" else "en", "-s", speed, "--stdin"], data

    @staticmethod
    def _feed(proc: subprocess.Popen, data: bytes) -> None:
        try:
            proc.stdin.write(data)
            proc.stdin.close()
        except OSError:
            pass

    def _start_external(self, text: str, lang: str) -> None:
        cmd, data = self._external_command(text, lang)
        kwargs = {
            "stdin": subprocess.PIPE if data is not None else subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._proc = subprocess.Popen(cmd, **kwargs)
        if data is not None:
            threading.Thread(target=self._feed, args=(self._proc, data), daemon=True).start()
        self._poll.start()
        self._set_speaking(True)

    def _check_process(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = None
            self._poll.stop()
            self._set_speaking(False)

    # --------------------------------------------------------- общий интерфейс

    def _set_speaking(self, value: bool) -> None:
        if value != self._speaking:
            self._speaking = value
            self.speaking_changed.emit(value)

    def is_speaking(self) -> bool:
        return self._speaking

    def speak(self, text: str) -> bool:
        """Озвучить ответ (разметка и ссылки убираются). True, если озвучка началась."""
        if not self.available:
            return False
        spoken = clean_for_speech(text)
        if not spoken:
            return False
        self.stop()
        lang = _language_of(spoken)
        try:
            if self.backend == "qt":
                self._set_qt_language(lang)
                self._qt.say(spoken)
            else:
                self._start_external(spoken, lang)
        except Exception:
            self._set_speaking(False)
            return False
        return True

    def stop(self) -> None:
        if self._qt is not None:
            try:
                self._qt.stop()
            except Exception:
                pass
        if self._proc is not None:
            try:
                self._proc.terminate()
            except OSError:
                pass
            self._proc = None
            self._poll.stop()
        self._set_speaking(False)
