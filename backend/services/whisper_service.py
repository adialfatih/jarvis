import os

from faster_whisper import WhisperModel


class WhisperService:
    def __init__(self):
        self.model = None
        self.model_size: str | None = None
        self.last_error: str | None = None
        self.is_loading = False

    @property
    def is_loaded(self) -> bool:
        return self.model is not None

    def load(self, model_size: str, model_path: str):
        self.model_size = model_size
        self.last_error = None
        self.is_loading = True

        try:
            self.model = WhisperModel(
                model_size,
                device="cpu",
                compute_type="int8",
                download_root=model_path,
            )
        except Exception as exc:
            self.model = None
            self.last_error = str(exc)
            raise
        finally:
            self.is_loading = False

    def transcribe(self, audio_path: str) -> str:
        if self.model is None:
            raise RuntimeError("Whisper model belum siap.")

        try:
            segments, _ = self.model.transcribe(audio_path, language="id")
            return " ".join(segment.text for segment in segments).strip()
        finally:
            if os.path.exists(audio_path):
                os.remove(audio_path)
