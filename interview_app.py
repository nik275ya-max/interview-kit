#!/usr/bin/env python3
"""InterviewKit — GUI на PyQt6.

Главное окно с тремя вкладками:
  * «Обработка»   — выбор файла, имя гостя, запуск подготовки, прогресс, лог.
  * «Расшифровка» — просмотр и правка плана монтажа перед сборкой.
  * «Настройки»   — параметры программы.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

import random

from PyQt6.QtCore import Qt, QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.config import Config
from core.editor import EditItem, Editor
from core.paths import (
    app_root,
    ensure_dirs,
    ffmpeg_path,
    incoming_dir,
    interviews_dir,
    interruptions_dir,
)
from core.pipeline import Pipeline


# --------------------------------------------------------------------------- #
#  Рабочий поток
# --------------------------------------------------------------------------- #

class WorkerSignals(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(float)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)
    ready_for_review = pyqtSignal(object, list)  # folder: Path, plan: List[EditItem]


class IncomingSignals(QObject):
    changed = pyqtSignal()


class PrepareWorker(QThread):
    """Фаза 1: подготовка видео (извлечение аудио, STT, план)."""

    def __init__(self, video_path: str, guest_name: str, config: Config):
        super().__init__()
        self.video_path = video_path
        self.guest_name = guest_name
        self.config = config
        self.signals = WorkerSignals()

    def run(self):
        try:
            pipe = Pipeline(
                self.config,
                log=self.signals.log.emit,
                progress=self.signals.progress.emit,
            )
            folder, plan = pipe.prepare(self.video_path, self.guest_name)
            self.signals.ready_for_review.emit(folder, plan)
        except Exception as ex:
            self.signals.failed.emit(f"{type(ex).__name__}: {ex}")


class AssembleWorker(QThread):
    """Фаза 2: сборка итогового видео по плану."""

    def __init__(self, folder: Path, plan: List[EditItem], config: Config):
        super().__init__()
        self.folder = folder
        self.plan = plan
        self.config = config
        self.signals = WorkerSignals()

    def run(self):
        try:
            pipe = Pipeline(
                self.config,
                log=self.signals.log.emit,
                progress=self.signals.progress.emit,
            )
            final = pipe.assemble(self.folder, self.plan)
            self.signals.finished_ok.emit(str(final))
        except Exception as ex:
            self.signals.failed.emit(f"{type(ex).__name__}: {ex}")


# --------------------------------------------------------------------------- #
#  Карточка элемента плана (для вкладки «Расшифровка»)
# --------------------------------------------------------------------------- #

class PlanItemWidget(QWidget):
    """Виджет одной строки плана: сегмент речи или перебивка."""

    removeRequested = pyqtSignal(int)       # индекс в плане
    replaceRequested = pyqtSignal(int)

    def __init__(self, index: int, item: EditItem, parent=None):
        super().__init__(parent)
        self.index = index
        self.item = item

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)

        if item.kind == "segment":
            mm_ss_start = _fmt_ts(item.start)
            mm_ss_end = _fmt_ts(item.end)
            title = QLabel(
                f"<b>[{mm_ss_start} – {mm_ss_end}] Ответ</b>"
            )
            title.setWordWrap(True)
            body = QLabel(item.text)
            body.setWordWrap(True)
            body.setStyleSheet("color: #222;")
            col = QVBoxLayout()
            col.addWidget(title)
            col.addWidget(body)
            lay.addLayout(col)
            lay.addStretch(1)
        else:
            name = os.path.basename(item.source_path)
            lbl = QLabel(
                f"<b>🎬 Перебивка</b> — {name}"
            )
            lbl.setWordWrap(True)
            lay.addWidget(lbl)
            lay.addStretch(1)
            btn_replace = QPushButton("Заменить")
            btn_replace.clicked.connect(lambda: self.replaceRequested.emit(self.index))
            lay.addWidget(btn_replace)
            btn_remove = QPushButton("Убрать")
            btn_remove.clicked.connect(lambda: self.removeRequested.emit(self.index))
            lay.addWidget(btn_remove)


def _fmt_ts(sec: float) -> str:
    m = int(sec // 60)
    s = int(sec % 60)
    return f"{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- #
#  Главное окно
# --------------------------------------------------------------------------- #

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("InterviewKit — авто-монтаж интервью с перебивками")
        self.resize(1000, 680)

        self.config = Config.load()
        self.current_folder: Path | None = None
        self.current_plan: List[EditItem] = []
        self.worker: QThread | None = None
        self._observer = None
        self._incoming_signals = IncomingSignals()
        self._incoming_signals.changed.connect(self.refresh_files)

        tabs = QTabWidget()
        tabs.addTab(self._build_process_tab(), "Обработка")
        tabs.addTab(self._build_review_tab(), "Расшифровка")
        tabs.addTab(self._build_settings_tab(), "Настройки")
        self.setCentralWidget(tabs)

        self.setStyleSheet(
            "QMainWindow { background: #fafafa; }"
            "QTextEdit { font-family: Consolas, 'Courier New'; font-size: 12px; }"
        )
        self._configure_watcher()

    # --- вкладка «Обработка» ---

    def _build_process_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        # Список файлов в incoming
        v.addWidget(QLabel("<b>Файлы в папке incoming:</b>"))
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.file_list.itemSelectionChanged.connect(self._clear_browsed_path)
        v.addWidget(self.file_list, 1)

        row = QHBoxLayout()
        self.btn_refresh = QPushButton("Обновить список")
        self.btn_refresh.clicked.connect(self.refresh_files)
        self.btn_browse = QPushButton("Выбрать файл…")
        self.btn_browse.clicked.connect(self.browse_file)
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_browse)
        row.addStretch(1)
        v.addLayout(row)

        # Имя гостя
        form = QHBoxLayout()
        form.addWidget(QLabel("Имя гостя:"))
        self.guest_edit = QLineEdit()
        self.guest_edit.setPlaceholderText("например: Иван и Мария")
        form.addWidget(self.guest_edit, 1)
        v.addLayout(form)

        # Кнопка запуска + прогресс
        self.btn_prepare = QPushButton("Подготовить расшифровку")
        self.btn_prepare.setStyleSheet("padding: 8px; font-weight: bold;")
        self.btn_prepare.clicked.connect(self.start_prepare)
        v.addWidget(self.btn_prepare)

        self.progress_label = QLabel("")
        v.addWidget(self.progress_label)

        # Лог
        v.addWidget(QLabel("<b>Лог:</b>"))
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        v.addWidget(self.log_view, 1)

        btn_open_incoming = QPushButton("Открыть папку incoming")
        btn_open_incoming.clicked.connect(lambda: _open_folder(incoming_dir()))
        btn_open_interviews = QPushButton("Открыть папку интервью")
        btn_open_interviews.clicked.connect(lambda: _open_folder(interviews_dir()))
        row2 = QHBoxLayout()
        row2.addWidget(btn_open_incoming)
        row2.addWidget(btn_open_interviews)
        row2.addStretch(1)
        v.addLayout(row2)

        self.refresh_files()
        return w

    # --- вкладка «Расшифровка» ---

    def _build_review_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(
            "<b>План монтажа</b> — проверьте расшифровку и перебивки перед сборкой. "
            "Перебивку можно заменить на другую или убрать."
        ))

        self.review_container = QWidget()
        self.review_layout = QVBoxLayout(self.review_container)
        self.review_layout.setContentsMargins(0, 0, 0, 0)
        self.review_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.review_container)
        v.addWidget(scroll, 1)

        self.lbl_summary = QLabel("")
        v.addWidget(self.lbl_summary)

        row = QHBoxLayout()
        self.btn_assemble = QPushButton("🎬 Собрать итоговое видео")
        self.btn_assemble.setStyleSheet("padding: 8px; font-weight: bold;")
        self.btn_assemble.clicked.connect(self.start_assemble)
        self.btn_assemble.setEnabled(False)
        row.addWidget(self.btn_assemble)
        row.addStretch(1)
        v.addLayout(row)

        return w

    # --- вкладка «Настройки» ---

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self.cb_whisper_model = QComboBox()
        self.cb_whisper_model.addItems(["tiny", "base", "small", "medium", "large-v3"])
        form.addRow("Модель Whisper:", self.cb_whisper_model)

        self.cb_whisper_device = QComboBox()
        self.cb_whisper_device.addItems(["cpu", "cuda"])
        form.addRow("Устройство Whisper:", self.cb_whisper_device)

        self.cb_compute_type = QComboBox()
        self.cb_compute_type.addItems(["int8", "float16", "float32"])
        self.cb_compute_type.setEditable(True)
        form.addRow("Точность вычислений:", self.cb_compute_type)

        self.le_language = QLineEdit("ru")
        form.addRow("Язык распознавания:", self.le_language)

        self.spin_silence_db = QDoubleSpinBox()
        self.spin_silence_db.setRange(-80, -5)
        self.spin_silence_db.setSingleStep(1)
        form.addRow("Порог тишины (dB):", self.spin_silence_db)

        self.spin_silence_dur = QDoubleSpinBox()
        self.spin_silence_dur.setRange(0.1, 5.0)
        self.spin_silence_dur.setSingleStep(0.1)
        self.spin_silence_dur.setSuffix(" сек")
        form.addRow("Мин. длительность паузы:", self.spin_silence_dur)

        self.spin_fallback = QDoubleSpinBox()
        self.spin_fallback.setRange(3.0, 60.0)
        self.spin_fallback.setSingleStep(1.0)
        self.spin_fallback.setSuffix(" сек")
        form.addRow("Отрезок при отсутствии пауз:", self.spin_fallback)

        self.cb_resolution = QComboBox()
        self.cb_resolution.addItems(["1920x1080", "1280x720", "3840x2160"])
        self.cb_resolution.setEditable(True)
        form.addRow("Разрешение выхода:", self.cb_resolution)

        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(10, 60)
        form.addRow("FPS выхода:", self.spin_fps)

        self.chk_watch = QCheckBox("Автоматически обновлять список файлов в incoming")
        form.addRow("", self.chk_watch)

        btn_save = QPushButton("Сохранить настройки")
        btn_save.clicked.connect(self.save_settings)
        form.addRow("", btn_save)

        btn_open_interruptions = QPushButton("Открыть папку перебивок")
        btn_open_interruptions.clicked.connect(lambda: _open_folder(interruptions_dir()))
        form.addRow("", btn_open_interruptions)

        self._settings_widgets = {
            "whisper_model": self.cb_whisper_model,
            "whisper_device": self.cb_whisper_device,
            "whisper_compute_type": self.cb_compute_type,
            "whisper_language": self.le_language,
            "silence_db": self.spin_silence_db,
            "silence_min_duration": self.spin_silence_dur,
            "fallback_segment_sec": self.spin_fallback,
            "output_resolution": self.cb_resolution,
            "output_fps": self.spin_fps,
            "watch_incoming": self.chk_watch,
        }
        self._load_settings_into_ui()
        return w

    def _load_settings_into_ui(self):
        c = self.config
        self.cb_whisper_model.setCurrentText(c.whisper_model)
        self.cb_whisper_device.setCurrentText(c.whisper_device)
        self.cb_compute_type.setCurrentText(c.whisper_compute_type)
        self.le_language.setText(c.whisper_language)
        self.spin_silence_db.setValue(c.silence_db)
        self.spin_silence_dur.setValue(c.silence_min_duration)
        self.spin_fallback.setValue(c.fallback_segment_sec)
        self.cb_resolution.setCurrentText(c.output_resolution)
        self.spin_fps.setValue(c.output_fps)
        self.chk_watch.setChecked(c.watch_incoming)

    def save_settings(self):
        c = self.config
        c.whisper_model = self.cb_whisper_model.currentText()
        c.whisper_device = self.cb_whisper_device.currentText()
        c.whisper_compute_type = self.cb_compute_type.currentText()
        c.whisper_language = self.le_language.text().strip() or "ru"
        c.silence_db = self.spin_silence_db.value()
        c.silence_min_duration = self.spin_silence_dur.value()
        c.fallback_segment_sec = self.spin_fallback.value()
        c.output_resolution = self.cb_resolution.currentText()
        c.output_fps = self.spin_fps.value()
        c.watch_incoming = self.chk_watch.isChecked()
        c.save()
        self._configure_watcher()
        QMessageBox.information(self, "Настройки", "Настройки сохранены в config.json")

    # --- действия ---

    def refresh_files(self):
        selected = self.file_list.currentItem()
        selected_name = selected.text() if selected else None
        self.file_list.clear()
        d = incoming_dir()
        d.mkdir(parents=True, exist_ok=True)
        for f in sorted(d.iterdir()):
            if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"):
                self.file_list.addItem(QListWidgetItem(f.name))
                if f.name == selected_name:
                    self.file_list.setCurrentRow(self.file_list.count() - 1)

    def browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбрать видео",
            str(incoming_dir()),
            "Видео (*.mp4 *.mov *.mkv *.avi *.webm *.m4v)",
        )
        if path:
            self.file_list.clear()
            self.file_list.addItem(QListWidgetItem(os.path.basename(path)))
            self._browsed_path = path
            self.guest_edit.setText(Path(path).stem)

    def _clear_browsed_path(self):
        # Смена выбора после диалога означает, что пользователь выбрал файл
        # из incoming, а не ранее указанный внешний путь.
        if self.file_list.hasFocus():
            self._browsed_path = None

    def _selected_video_path(self) -> str | None:
        if hasattr(self, "_browsed_path") and self._browsed_path:
            p = self._browsed_path
            self._browsed_path = None
            return p
        item = self.file_list.currentItem()
        if not item:
            return None
        return str(incoming_dir() / item.text())

    def start_prepare(self):
        path = self._selected_video_path()
        if not path:
            QMessageBox.warning(self, "Нет файла", "Выберите видеофайл в списке или через «Выбрать файл…».")
            return
        if not os.path.exists(path):
            QMessageBox.warning(self, "Файл не найден", path)
            return
        guest = self.guest_edit.text().strip()
        if not guest:
            QMessageBox.warning(self, "Имя гостя", "Укажите имя гостя.")
            return

        self.log_view.clear()
        self.progress_label.setText("0%")
        self.btn_prepare.setEnabled(False)
        self.btn_assemble.setEnabled(False)

        self.worker = PrepareWorker(path, guest, self.config)
        self.worker.signals.log.connect(self.log_view.append)
        self.worker.signals.progress.connect(self._on_progress)
        self.worker.signals.ready_for_review.connect(self._on_ready_for_review)
        self.worker.signals.failed.connect(self._on_failed)
        self.worker.start()

    def _on_progress(self, p: float):
        self.progress_label.setText(f"{int(p * 100)}%")

    def _on_failed(self, msg: str):
        self.log_view.append(f"\n❌ Ошибка: {msg}")
        self.btn_prepare.setEnabled(True)
        self.btn_assemble.setEnabled(bool(self.current_folder and self.current_plan))
        self.worker = None

    def _on_ready_for_review(self, folder: Path, plan: list):
        self.current_folder = folder
        self.current_plan = [EditItem.from_dict(it.to_dict()) for it in plan]
        self.btn_prepare.setEnabled(True)
        self.btn_assemble.setEnabled(True)
        self.log_view.append(f"\n✓ Подготовка завершена. Можно править перебивки во вкладке «Расшифровка».")
        self._populate_review()
        self.worker = None

    def _populate_review(self):
        # очистка
        while self.review_layout.count():
            child = self.review_layout.takeAt(0)
            w = child.widget()
            if w:
                w.deleteLater()

        for i, item in enumerate(self.current_plan):
            widget = PlanItemWidget(i, item)
            widget.removeRequested.connect(self._remove_item)
            widget.replaceRequested.connect(self._replace_item)
            self.review_layout.addWidget(widget)

        segs = sum(1 for it in self.current_plan if it.kind == "segment")
        inters = sum(1 for it in self.current_plan if it.kind == "interruption")
        self.lbl_summary.setText(f"Сегментов: {segs} · Перебивок: {inters}")

    def _remove_item(self, idx: int):
        if 0 <= idx < len(self.current_plan) and self.current_plan[idx].kind == "interruption":
            del self.current_plan[idx]
            self._populate_review()

    def _replace_item(self, idx: int):
        if not (0 <= idx < len(self.current_plan)):
            return
        item = self.current_plan[idx]
        if item.kind != "interruption":
            return
        files = Editor.list_interruptions()
        alternatives = [path for path in files if path != item.source_path]
        item.source_path = random.choice(alternatives or files) or item.source_path
        self._populate_review()

    def start_assemble(self):
        if not self.current_folder or not self.current_plan:
            return
        self.log_view.append("")
        self.btn_assemble.setEnabled(False)

        self.worker = AssembleWorker(self.current_folder, self.current_plan, self.config)
        self.worker.signals.log.connect(self.log_view.append)
        self.worker.signals.progress.connect(self._on_progress)
        self.worker.signals.finished_ok.connect(self._on_assemble_done)
        self.worker.signals.failed.connect(self._on_failed)
        self.worker.start()

    def _configure_watcher(self):
        """Включает или выключает наблюдение за папкой incoming."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
        if not self.config.watch_incoming:
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            emit_changed = self._incoming_signals.changed.emit

            class Handler(FileSystemEventHandler):
                def on_created(self, event):
                    if not event.is_directory:
                        emit_changed()

                def on_deleted(self, event):
                    if not event.is_directory:
                        emit_changed()

                def on_moved(self, event):
                    if not event.is_directory:
                        emit_changed()

            observer = Observer()
            observer.schedule(Handler(), str(incoming_dir()), recursive=False)
            observer.start()
            self._observer = observer
        except Exception as ex:
            self.log_view.append(f"⚠ Не удалось включить наблюдение за incoming: {ex}")
            self.chk_watch.setChecked(False)

    def closeEvent(self, event):
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
        super().closeEvent(event)

    def _on_assemble_done(self, final_path: str):
        self.log_view.append(f"\n✅ Готово: {final_path}")
        self.btn_assemble.setEnabled(True)
        self.progress_label.setText("100% — готово")
        QMessageBox.information(
            self, "Готово",
            f"Итоговое видео собрано:\n{final_path}\n\nОткрыть папку интервью?",
        )
        _open_folder(Path(final_path).parent)
        self.worker = None


# --------------------------------------------------------------------------- #
#  Утилиты
# --------------------------------------------------------------------------- #

def _open_folder(path: Path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        os.system(f'open "{path}"')
    else:
        os.system(f'xdg-open "{path}"')


def main():
    ensure_dirs()
    # проверяем ffmpeg
    if ffmpeg_path() == "ffmpeg":
        print("Внимание: ffmpeg.exe не найден рядом с программой, "
              "используется системный ffmpeg (если есть).")

    app = QApplication(sys.argv)
    app.setApplicationName("InterviewKit")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
