"""Локальный голосовой ввод для PySide6: микрофон + Qwen3-ASR.

Зависимости: pip install sounddevice qwen-asr
Модель загружается при первой расшифровке и затем кешируется в памяти.

Настройки (необязательные переменные окружения):
  QWEN_ASR_MODEL    Qwen/Qwen3-ASR-0.6B (или путь к скачанной модели)
  QWEN_ASR_DEVICE   auto | cpu | cuda (по умолчанию auto)
  QWEN_ASR_LANGUAGE auto | Russian | English | ... (по умолчанию auto)
"""
from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QThread, Signal

MAX_RECORD_SECONDS = 120
DEFAULT_MODEL = "Qwen/Qwen3-ASR-0.6B"
_asr_model = None
_asr_config = None


class VoiceRecordWorker(QThread):
    """Записывает моно-аудио до повторного нажатия кнопки (макс. 120 с)."""

    recording_started = Signal()
    audio_ready = Signal(object, int)  # numpy.ndarray, sample_rate
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            self.failed.emit(f"Не установлена библиотека записи звука: {exc}. Выполните: pip install sounddevice")
            return

        try:
            device = sd.query_devices(kind="input")
            sample_rate = int(device["default_samplerate"])
            if sample_rate <= 0:
                sample_rate = 16000
            chunks = []

            def callback(indata, _frames, _time_info, _status) -> None:
                # Callback вызывается PortAudio: никаких операций Qt внутри него.
                chunks.append(indata[:, 0].copy())

            with sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                callback=callback,
            ):
                self.recording_started.emit()
                deadline = time.monotonic() + MAX_RECORD_SECONDS
                while not self._stop_event.wait(0.1):
                    if time.monotonic() >= deadline:
                        break

            if not chunks:
                self.failed.emit("Звук не записан. Проверьте микрофон и разрешения доступа к нему.")
                return
            audio = np.concatenate(chunks).astype(np.float32, copy=False)
            if len(audio) < int(sample_rate * 0.25):
                self.failed.emit("Запись слишком короткая. Попробуйте говорить чуть дольше.")
                return
            self.audio_ready.emit(audio, sample_rate)
        except Exception as exc:
            self.failed.emit(f"Не удалось записать звук: {exc}. Проверьте микрофон и доступ к нему.")


def _get_model():
    """Создаёт Qwen3-ASR при первом вызове; следующие вызовы используют тот же объект."""
    global _asr_model, _asr_config
    import torch
    from qwen_asr import Qwen3ASRModel

    model_path = os.environ.get("QWEN_ASR_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    device_setting = os.environ.get("QWEN_ASR_DEVICE", "auto").strip().lower()
    if device_setting not in {"auto", "cpu", "cuda"}:
        raise ValueError("QWEN_ASR_DEVICE должен быть: auto, cpu или cuda")
    use_cuda = (torch.cuda.is_available() if device_setting == "auto"
                else device_setting == "cuda")
    if use_cuda and not torch.cuda.is_available():
        raise RuntimeError("CUDA недоступна. Установите QWEN_ASR_DEVICE=cpu")

    config = (model_path, use_cuda)
    if _asr_model is None or _asr_config != config:
        _asr_model = Qwen3ASRModel.from_pretrained(
            model_path,
            dtype=torch.float16 if use_cuda else torch.float32,
            device_map="cuda:0" if use_cuda else "cpu",
            max_inference_batch_size=1,
            max_new_tokens=512,
        )
        _asr_config = config
    return _asr_model


class TranscribeWorker(QThread):
    """Расшифровывает готовую запись без блокирования интерфейса."""

    done = Signal(str)
    failed = Signal(str)

    def __init__(self, audio, sample_rate: int) -> None:
        super().__init__()
        self.audio = audio
        self.sample_rate = int(sample_rate)

    def run(self) -> None:
        try:
            model = _get_model()
            language = os.environ.get("QWEN_ASR_LANGUAGE", "auto").strip()
            if language.lower() in ("auto", ""):
                language = None
            result = model.transcribe(
                audio=(self.audio, self.sample_rate),
                language=language,
            )
            text = result[0].text.strip() if result else ""
            if not text:
                self.failed.emit("Модель не распознала речь. Попробуйте говорить чётче.")
                return
            self.done.emit(text)
        except ImportError as exc:
            self.failed.emit(f"Не установлена библиотека для распознавания: {exc}. Выполните: pip install qwen-asr")
        except Exception as exc:
            self.failed.emit(f"Ошибка распознавания Qwen3-ASR: {exc}")
