"""Приложение: тёмный неоновый чат с локальной моделью Qwen.

Запуск вручную:  python app.py
Обычно запускается двойным кликом по ярлыку на рабочем столе.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

import ai_client
from tts import Speaker
from voice_input import VoiceRecordWorker, TranscribeWorker

APP_NAME = "Внучечек"
SUBTITLE = "локальная нейросеть Qwen"
WELCOME = (
    "Привет! 👋 Я Qwen и работаю прямо на твоём компьютере, без облака. "
    "Спрашивай что угодно: могу поискать в интернете или открыть сайт."
)
PLACEHOLDER = "Напиши что-нибудь…"

# Быстрые подсказки: (текст на кнопке, что отправится модели)
CHIPS = [
    ("📅  Какой сегодня день?", "Какой сегодня день?"),
    ("📰  Что нового?", "Какие сегодня главные новости?"),
    ("▶  Открой YouTube", "Открой youtube"),
    ("😄  Расскажи шутку", "Расскажи короткую шутку"),
]

# ------------------------------------------------------------------- стиль

ACCENT = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #8b5cf6, stop:1 #ec4899)"
ACCENT_HOVER = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #a78bfa, stop:1 #f472b6)"
ACCENT_PRESSED = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #7c3aed, stop:1 #db2777)"

STYLE = """
QWidget#root {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                stop:0 #0a0a14, stop:0.55 #12102a, stop:1 #1b0f2e);
}

QFrame#header {
    background: rgba(255, 255, 255, 4%);
    border: 1px solid rgba(255, 255, 255, 8%);
    border-radius: 18px;
}
QLabel#title    { color: #ffffff; font-size: 18px; font-weight: 700; }
QLabel#subtitle { color: #8b8ba8; font-size: 12px; }
QLabel#statusText { color: #b4b4cc; font-size: 12px; }

QPushButton#ghost {
    color: #b4b4cc; background: transparent;
    border: 1px solid #2f2f4a; border-radius: 12px;
    padding: 6px 12px; font-size: 12px;
}
QPushButton#ghost:hover { background: rgba(255, 255, 255, 8%); color: #ffffff; }
QPushButton#ghost[speaking="true"] {
    color: #ffffff; background: rgba(139, 92, 246, 35%); border-color: #8b5cf6;
}
QPushButton#ghost:disabled { color: #5c5c78; border-color: #2a2a40; }

QScrollArea { background: transparent; border: none; }
QWidget#chatHost { background: transparent; }

QLabel#userBubble {
    background: %ACCENT%; color: #ffffff;
    border-radius: 18px; border-bottom-right-radius: 5px;
    padding: 10px 14px;
}
QLabel#botBubble {
    background: #1a1a2e; color: #ececf8;
    border: 1px solid #2a2a45;
    border-radius: 18px; border-bottom-left-radius: 5px;
    padding: 10px 14px;
}
QLabel#typingBubble {
    background: #1a1a2e; color: #a78bfa;
    border: 1px solid #2a2a45;
    border-radius: 18px; border-bottom-left-radius: 5px;
    padding: 10px 14px;
}
QLabel#errorBubble {
    background: #2a1420; color: #ffb4c4;
    border: 1px solid #5c2236;
    border-radius: 18px; border-bottom-left-radius: 5px;
    padding: 10px 14px;
}

QLabel#hint { color: #7a7a99; font-size: 12px; }
QPushButton#chip {
    color: #d8d8f0; background: rgba(255, 255, 255, 5%);
    border: 1px solid #34345a; border-radius: 16px;
    padding: 8px 14px; font-size: 13px; text-align: left;
}
QPushButton#chip:hover {
    background: rgba(139, 92, 246, 22%); border-color: #8b5cf6; color: #ffffff;
}

QFrame#inputBar {
    background: #15152a; border: 1px solid #2e2e50; border-radius: 28px;
}
QLineEdit#input {
    background: transparent; border: none; color: #f2f2fa;
    font-size: 15px; selection-background-color: #8b5cf6;
}
QPushButton#send {
    color: #ffffff; background: %ACCENT%; border: none;
    border-radius: 22px; font-size: 22px; font-weight: 700;
}
QPushButton#send:hover    { background: %ACCENT_HOVER%; }
QPushButton#send:pressed  { background: %ACCENT_PRESSED%; }
QPushButton#send:disabled { background: #2a2a45; color: #6b6b8a; }
QPushButton#mic {
    color: #d7c4ff; background: #292041;
    border: 1px solid #4c3976; border-radius: 21px;
    font-size: 20px;
}
QPushButton#mic:hover { background: #3c2b63; color: #ffffff; }
QPushButton#mic[recording="true"] {
    color: #ffffff; background: #ae285c; border: 1px solid #f472b6;
}
QPushButton#mic:disabled { background: #242439; color: #74748c; border-color: #33334e; }
QLabel#voiceHint { color: #b6a4da; font-size: 12px; padding: 0 8px; }

QScrollBar:vertical { background: transparent; width: 8px; margin: 4px 2px; }
QScrollBar::handle:vertical { background: #2f2f4a; border-radius: 3px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #45456b; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
""".replace("%ACCENT_HOVER%", ACCENT_HOVER).replace("%ACCENT_PRESSED%", ACCENT_PRESSED).replace("%ACCENT%", ACCENT)


def resource_path(name: str) -> Path:
    """Путь к файлу рядом с программой (работает и в .exe от PyInstaller)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


# ------------------------------------------------------------------ потоки

class AskWorker(QThread):
    """Запрос к модели в отдельном потоке, чтобы окно не зависало."""
    done = Signal(str)

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def run(self) -> None:
        try:
            self.done.emit(ai_client.ask(self.prompt))
        except Exception as e:
            self.done.emit(
                f"Ошибка: {e}\n\nПроверьте, что установлен llama-cpp-python "
                "и модель скачана (папка models)."
            )


class WarmupWorker(QThread):
    """Заранее загружает модель, пока пользователь читает приветствие."""
    ready = Signal(bool)

    def run(self) -> None:
        try:
            warmup = getattr(ai_client, "warmup", None)
            if warmup:
                warmup()
            self.ready.emit(True)
        except Exception:
            self.ready.emit(False)


# ----------------------------------------------------------------- пузырь

BUBBLE_PAD_X = 14   # как padding в стиле
BUBBLE_FONT_PX = 14


class Bubble(QWidget):
    """Строка чата: один пузырь сообщения, слева (бот) или справа (пользователь)."""

    NAMES = {
        "user": "userBubble", "bot": "botBubble",
        "error": "errorBubble", "typing": "typingBubble",
    }

    def __init__(self, text: str, kind: str = "bot") -> None:
        super().__init__()
        self.plain = text

        self.label = QLabel()
        self.label.setObjectName(self.NAMES[kind])
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self.label.setOpenExternalLinks(True)
        self.label.setTextFormat(
            Qt.TextFormat.MarkdownText if kind == "bot" else Qt.TextFormat.PlainText
        )
        font = QFont(self.label.font())
        font.setPixelSize(BUBBLE_FONT_PX)
        self.label.setFont(font)
        self.label.setText(text)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        if kind == "user":
            row.addStretch(1)
            row.addWidget(self.label)
        else:
            row.addWidget(self.label)
            row.addStretch(1)

    def set_text(self, text: str) -> None:
        self.plain = text
        self.label.setText(text)

    def fit(self, max_width: int) -> None:
        """Ширина пузыря по тексту, но не больше max_width (дальше текст переносится)."""
        fm = self.label.fontMetrics()
        longest = max((fm.horizontalAdvance(line) for line in self.plain.split("\n")), default=0)
        need = longest + 2 * BUBBLE_PAD_X + 12
        self.label.setFixedWidth(max(60, min(max_width, need)))


# ------------------------------------------------------------------- окно

class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("root")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(STYLE)

        self.busy = False
        self.voice_recording = False
        self.voice_transcribing = False
        self.voice_recorder: VoiceRecordWorker | None = None
        self.voice_worker: TranscribeWorker | None = None
        self._closing = False
        self.bubbles: list[Bubble] = []
        self.typing: Bubble | None = None
        self._typing_frame = 0
        self._titlebar_done = False

        # озвучка ответов (включена по умолчанию, выбор запоминается)
        self.settings = QSettings("QwenAssistant", "Assistant")
        self.tts_enabled = self.settings.value("tts_enabled", True, type=bool)
        self.speaker = Speaker(self)
        self.speaker.speaking_changed.connect(lambda _speaking: self._refresh_tts_button())

        self.setWindowTitle(APP_NAME)
        self.resize(460, 720)
        self.setMinimumSize(360, 480)
        icon = resource_path("icon.icns" if sys.platform == "darwin" else "icon.ico")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        # ---- шапка
        header = QFrame()
        header.setObjectName("header")
        h = QHBoxLayout(header)
        h.setContentsMargins(18, 12, 12, 12)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        subtitle = QLabel(SUBTITLE)
        subtitle.setObjectName("subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)

        self.dot = QLabel("●")
        self.status_text = QLabel("")
        self.status_text.setObjectName("statusText")

        clear_btn = QPushButton("Очистить")
        clear_btn.setObjectName("ghost")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setToolTip("Начать новый диалог")
        clear_btn.clicked.connect(lambda: self.clear_chat())

        self.tts_btn = QPushButton("🔊")
        self.tts_btn.setObjectName("ghost")
        self.tts_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tts_btn.setProperty("speaking", False)
        self.tts_btn.setFixedWidth(42)
        self.tts_btn.clicked.connect(self.on_tts_clicked)

        h.addLayout(titles)
        h.addStretch(1)
        h.addWidget(self.dot)
        h.addWidget(self.status_text)
        h.addSpacing(8)
        h.addWidget(self.tts_btn)
        h.addWidget(clear_btn)

        # ---- область сообщений
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setStyleSheet("background: transparent;")
        host = QWidget()
        host.setObjectName("chatHost")
        self.chat = QVBoxLayout(host)
        self.chat.setContentsMargins(4, 8, 4, 8)
        self.chat.setSpacing(10)
        self.chat.addStretch(1)
        self.scroll.setWidget(host)
        bar = self.scroll.verticalScrollBar()
        bar.rangeChanged.connect(lambda _lo, _hi: bar.setValue(bar.maximum()))

        # ---- быстрые подсказки
        self.chips_box = QWidget()
        cv = QVBoxLayout(self.chips_box)
        cv.setContentsMargins(4, 0, 4, 0)
        cv.setSpacing(8)
        hint = QLabel("Попробуй:")
        hint.setObjectName("hint")
        cv.addWidget(hint)
        grid = QGridLayout()
        grid.setSpacing(8)
        for i, (label, prompt) in enumerate(CHIPS):
            chip = QPushButton(label)
            chip.setObjectName("chip")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _=False, p=prompt: self.submit(p))
            grid.addWidget(chip, i // 2, i % 2)
        cv.addLayout(grid)

        # ---- строка ввода
        input_bar = QFrame()
        input_bar.setObjectName("inputBar")
        ib = QHBoxLayout(input_bar)
        ib.setContentsMargins(20, 6, 6, 6)
        ib.setSpacing(8)

        self.input = QLineEdit()
        self.input.setObjectName("input")
        self.input.setPlaceholderText(PLACEHOLDER)
        self.input.setMinimumHeight(40)
        palette = self.input.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#6b6b8a"))
        self.input.setPalette(palette)
        self.input.returnPressed.connect(lambda: self.submit())

        self.send_btn = QPushButton("↑")
        self.send_btn.setObjectName("send")
        self.send_btn.setFixedSize(44, 44)
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(lambda: self.submit())
        glow = QGraphicsDropShadowEffect(self)
        glow.setBlurRadius(28)
        glow.setOffset(0, 4)
        glow.setColor(QColor(139, 92, 246, 170))
        self.send_btn.setGraphicsEffect(glow)

        self.mic_btn = QPushButton("🎙")
        self.mic_btn.setObjectName("mic")
        self.mic_btn.setFixedSize(42, 42)
        self.mic_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic_btn.setToolTip("Начать голосовой ввод")
        self.mic_btn.setProperty("recording", False)
        self.mic_btn.clicked.connect(self.toggle_voice)

        ib.addWidget(self.input, 1)
        ib.addWidget(self.mic_btn)
        ib.addWidget(self.send_btn)

        self.voice_hint = QLabel("")
        self.voice_hint.setObjectName("voiceHint")
        self.voice_hint.setWordWrap(True)
        self.voice_hint.hide()

        # ---- сборка
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)
        root.addWidget(header)
        root.addWidget(self.scroll, 1)
        root.addWidget(self.chips_box)
        root.addWidget(input_bar)
        root.addWidget(self.voice_hint)

        # анимация «печатает…»
        self.typing_timer = QTimer(self)
        self.typing_timer.setInterval(350)
        self.typing_timer.timeout.connect(self._tick_typing)

        self.add_bubble(WELCOME, "bot")
        self.input.setFocus()
        self._refresh_tts_button()

        # заранее грузим модель
        self.set_status("loading")
        self.warmup = WarmupWorker()
        self.warmup.ready.connect(lambda ok: self.set_status("ready" if ok else "error"))
        self.warmup.start()

        if self.speaker.available and not self.speaker.has_russian_voice:
            QTimer.singleShot(600, lambda: self._voice_message(
                "Для озвучки не найден русский голос. Windows: Параметры → Время и язык → "
                "Речь → Добавить голоса."))

    # ---------------------------------------------------------- статус

    def set_status(self, state: str) -> None:
        color, text = {
            "loading": ("#fbbf24", "Загрузка модели…"),
            "ready": ("#34d399", "Онлайн"),
            "error": ("#f87171", "Модель не загружена"),
        }[state]
        self.dot.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.status_text.setText(text)

    # -------------------------------------------------------- сообщения

    def _max_bubble_width(self) -> int:
        vw = self.scroll.viewport().width()
        if vw < 200:   # до первого показа окна ширина ещё не известна
            vw = max(self.width() - 40, 300)
        return int(vw * 0.82)

    def add_bubble(self, text: str, kind: str) -> Bubble:
        bubble = Bubble(text, kind)
        bubble.fit(self._max_bubble_width())
        self.chat.insertWidget(self.chat.count() - 1, bubble)   # перед растяжкой
        self.bubbles.append(bubble)
        return bubble

    def _remove_bubble(self, bubble: Bubble) -> None:
        if bubble in self.bubbles:
            self.bubbles.remove(bubble)
        self.chat.removeWidget(bubble)
        bubble.deleteLater()

    def _refit(self) -> None:
        width = self._max_bubble_width()
        for bubble in self.bubbles:
            bubble.fit(width)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refit()
        if not self._titlebar_done:
            self._titlebar_done = True
            self.apply_dark_titlebar()

    def clear_chat(self) -> None:
        if self.busy or self.voice_recording or self.voice_transcribing:
            return
        self.speaker.stop()
        for bubble in list(self.bubbles):
            self._remove_bubble(bubble)
        reset = getattr(ai_client, "reset_history", None)
        if reset:
            reset()
        self.add_bubble(WELCOME, "bot")
        self.chips_box.show()
        self.input.setFocus()

    # ---------------------------------------------------- отправка/ответ

    def _update_input_controls(self) -> None:
        self.send_btn.setEnabled(not (self.busy or self.voice_recording or self.voice_transcribing))
        self.input.setEnabled(not (self.voice_recording or self.voice_transcribing))
        self.mic_btn.setEnabled(not (self.busy or self.voice_transcribing))

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._update_input_controls()

    def submit(self, text: str | None = None) -> None:
        if self.busy or self.voice_recording or self.voice_transcribing:
            return
        text = (text if text is not None else self.input.text()).strip()
        if not text:
            return
        self.speaker.stop()
        self.input.clear()
        self.chips_box.hide()
        self.add_bubble(text, "user")
        self.show_typing()
        self.set_busy(True)

        self.worker = AskWorker(text)
        self.worker.done.connect(self.on_answer)
        self.worker.start()

    def show_typing(self) -> None:
        self._typing_frame = 0
        self.typing = self.add_bubble("●  ○  ○", "typing")
        self.typing_timer.start()

    def hide_typing(self) -> None:
        self.typing_timer.stop()
        if self.typing is not None:
            self._remove_bubble(self.typing)
            self.typing = None

    def _tick_typing(self) -> None:
        if self.typing is None:
            return
        frames = ("●  ○  ○", "○  ●  ○", "○  ○  ●")
        self._typing_frame = (self._typing_frame + 1) % len(frames)
        self.typing.set_text(frames[self._typing_frame])

    def on_answer(self, text: str) -> None:
        self.hide_typing()
        is_error = text.startswith("Ошибка")
        self.add_bubble(text or "(пустой ответ)", "error" if is_error else "bot")
        self.set_busy(False)
        self.input.setFocus()
        if text and not is_error:
            self._speak_answer(text)

        consume = getattr(ai_client, "consume_opened", None)
        if consume and consume():
            # Chrome забирает фокус не мгновенно: проверяем несколько раз
            for ms in (800, 1600, 2800, 4000):
                QTimer.singleShot(ms, self._raise_if_needed)

    # ------------------------------------------------------------ озвучка

    def _refresh_tts_button(self) -> None:
        speaking = self.speaker.is_speaking()
        if not self.speaker.available:
            text, tip = "🔇", "Озвучка недоступна на этом компьютере"
        elif speaking:
            text, tip = "⏹", "Остановить озвучку"
        elif self.tts_enabled:
            text, tip = "🔊", "Озвучка ответов включена (нажмите, чтобы выключить)"
        else:
            text, tip = "🔇", "Озвучка выключена (нажмите, чтобы включить)"
        self.tts_btn.setText(text)
        self.tts_btn.setToolTip(tip)
        self.tts_btn.setEnabled(self.speaker.available)
        self.tts_btn.setProperty("speaking", speaking)
        self.tts_btn.style().unpolish(self.tts_btn)
        self.tts_btn.style().polish(self.tts_btn)

    def on_tts_clicked(self) -> None:
        if self.speaker.is_speaking():
            self.speaker.stop()    # только остановить текущую озвучку
        else:
            self.tts_enabled = not self.tts_enabled
            self.settings.setValue("tts_enabled", self.tts_enabled)
        self._refresh_tts_button()

    def _speak_answer(self, text: str) -> None:
        # во время записи голоса не озвучиваем, чтобы микрофон не слышал ответ
        if (self.tts_enabled and self.speaker.available and not self._closing
                and not (self.voice_recording or self.voice_transcribing)):
            self.speaker.speak(text)

    # ------------------------------------------------------ голосовой ввод

    def _voice_message(self, message: str, error: bool = False) -> None:
        self.voice_hint.setText(message)
        self.voice_hint.setStyleSheet("color: #fca5a5;" if error else "")
        self.voice_hint.setVisible(bool(message))

    def _set_microphone_recording(self, recording: bool) -> None:
        self.mic_btn.setText("■" if recording else "🎙")
        self.mic_btn.setToolTip("Остановить запись" if recording else "Начать голосовой ввод")
        self.mic_btn.setProperty("recording", recording)
        self.mic_btn.style().unpolish(self.mic_btn)
        self.mic_btn.style().polish(self.mic_btn)

    def toggle_voice(self) -> None:
        if self.busy or self.voice_transcribing:
            return
        if self.voice_recording:
            self.voice_recording = False
            self.voice_transcribing = True   # блокируем новый ввод до получения аудио
            self._set_microphone_recording(False)
            self._update_input_controls()
            self._voice_message("Завершаю запись…")
            if self.voice_recorder:
                self.voice_recorder.request_stop()
            return

        self.speaker.stop()    # чтобы микрофон не записывал озвучку
        self.voice_recording = True
        self._set_microphone_recording(True)
        self._update_input_controls()
        self._voice_message("Подключение к микрофону…")
        self.voice_recorder = VoiceRecordWorker()
        self.voice_recorder.recording_started.connect(self._on_recording_started)
        self.voice_recorder.audio_ready.connect(self._on_voice_audio)
        self.voice_recorder.failed.connect(self._on_voice_error)
        self.voice_recorder.finished.connect(self._on_voice_recorder_finished)
        self.voice_recorder.start()

    def _on_recording_started(self) -> None:
        if self.voice_recording:
            self._voice_message("🔴 Идёт запись. Нажмите ■, чтобы расшифровать речь.")

    def _on_voice_audio(self, audio, sample_rate: int) -> None:
        if self._closing:
            return
        self.voice_recording = False
        self.voice_transcribing = True
        self._set_microphone_recording(False)
        self._update_input_controls()
        self._voice_message("Распознаю речь через Qwen3-ASR… (при первом запуске скачивается модель)")
        self.voice_worker = TranscribeWorker(audio, sample_rate)
        self.voice_worker.done.connect(self._on_voice_transcribed)
        self.voice_worker.failed.connect(self._on_voice_error)
        self.voice_worker.finished.connect(self._on_voice_worker_finished)
        self.voice_worker.start()

    def _on_voice_transcribed(self, text: str) -> None:
        # Не отправляем автоматически: пользователь может исправить транскрипцию.
        old_text = self.input.text().strip()
        self.input.setText((old_text + " " + text).strip() if old_text else text)
        self.voice_recording = False
        self.voice_transcribing = False
        self._update_input_controls()
        self._voice_message("Текст распознан. Проверьте его и нажмите ↑ для отправки.")
        self.input.setFocus()

    def _on_voice_error(self, message: str) -> None:
        self.voice_recording = False
        self.voice_transcribing = False
        self._set_microphone_recording(False)
        self._update_input_controls()
        self._voice_message(message, error=True)
        self.input.setFocus()

    def _on_voice_recorder_finished(self) -> None:
        worker = self.sender()
        if self.voice_recorder is worker:
            self.voice_recorder = None
        worker.deleteLater()

    def _on_voice_worker_finished(self) -> None:
        worker = self.sender()
        if self.voice_worker is worker:
            self.voice_worker = None
        worker.deleteLater()

    def closeEvent(self, event) -> None:
        # Записывающему потоку нужно дать закрыть микрофон до уничтожения окна.
        self._closing = True
        if self.voice_recorder and self.voice_recorder.isRunning():
            self.voice_recorder.request_stop()
            if not self.voice_recorder.wait(3000):
                self._closing = False
                event.ignore()
                self._voice_message("Микрофон ещё закрывается. Повторите закрытие окна.")
                return
        if self.voice_worker and self.voice_worker.isRunning():
            self._closing = False
            event.ignore()
            self._voice_message("Идёт расшифровка. Закройте окно после её завершения.")
            return
        self.speaker.stop()
        super().closeEvent(event)

    # ------------------------------------------------- подъём окна наверх

    def _raise_if_needed(self) -> None:
        if not self.isActiveWindow():   # фокус ушёл в другое приложение
            self.bring_to_front()

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()

        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.BringWindowToTop.argtypes = [wintypes.HWND]
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT,
            ]

            hwnd = wintypes.HWND(int(self.winId()))
            HWND_TOPMOST, HWND_NOTOPMOST = wintypes.HWND(-1), wintypes.HWND(-2)
            flags = 0x0001 | 0x0002 | 0x0040   # NOSIZE | NOMOVE | SHOWWINDOW

            fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            my_thread = kernel32.GetCurrentThreadId()
            attached = bool(fg_thread and fg_thread != my_thread
                            and user32.AttachThreadInput(fg_thread, my_thread, True))

            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)

            if attached:
                user32.AttachThreadInput(fg_thread, my_thread, False)

        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    # ---------------------------------------------- тёмный заголовок окна

    def apply_dark_titlebar(self) -> None:
        """Тёмная рамка окна в Windows 10/11, чтобы заголовок не выбивался из дизайна."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute.argtypes = [
                wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ]
            hwnd = wintypes.HWND(int(self.winId()))
            one = ctypes.c_int(1)
            for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE
                if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(one), ctypes.sizeof(one)) == 0:
                    break
            caption = ctypes.c_uint(0x00140A0A)   # Windows 11: цвет заголовка #0a0a14 (BGR)
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
        except Exception:
            pass


def main() -> int:
    if sys.platform == "win32":
        # Чтобы в панели задач была наша иконка, а не иконка Python.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("my.desktop.app.1")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")   # одинаковый вид на всех системах, стили применяются предсказуемо
    app.setApplicationName(APP_NAME)

    font = QFont()
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Inter", "Arial"])
    font.setPointSize(10)
    app.setFont(font)

    icon = resource_path("icon.icns" if sys.platform == "darwin" else "icon.ico")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))  # иконка в Dock / панели задач

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
