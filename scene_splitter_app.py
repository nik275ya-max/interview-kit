#!/usr/bin/env python3
"""PyQt-интерфейс для нарезки видео по сценам."""
from __future__ import annotations

import sys
from pathlib import Path
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
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
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core.ffmpeg_wrapper import FFmpeg, FFmpegError
from core.paths import ffmpeg_path
from core.scene_splitter import (
    SceneSplitter,
    VIDEO_EXTS,
    collect_videos,
    content_range,
    make_boundaries,
    scene_ranges,
)


class WorkerSignals(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    preview = pyqtSignal(str, object)
    finished = pyqtSignal()
    failed = pyqtSignal(str)


class SceneWorker(QThread):
    def __init__(
        self,
        paths: list[Path],
        output_root: Path | None,
        threshold: float,
        min_duration: float,
        copy_streams: bool,
        trim_black: bool,
        analyze_only: bool,
        overwrite: bool,
    ):
        super().__init__()
        self.paths = paths
        self.output_root = output_root
        self.threshold = threshold
        self.min_duration = min_duration
        self.copy_streams = copy_streams
        self.trim_black = trim_black
        self.analyze_only = analyze_only
        self.overwrite = overwrite
        self.signals = WorkerSignals()

    @staticmethod
    def output_dir(video: Path, root: Path | None, multiple: bool) -> Path:
        if root is None:
            return video.parent / f"{video.stem}_scenes"
        return root / video.stem if multiple else root

    def run(self):
        splitter = SceneSplitter(FFmpeg(ffmpeg_path()))
        try:
            for index, video in enumerate(self.paths, 1):
                self.signals.log.emit(f"\n[{index}/{len(self.paths)}] {video}")
                if self.analyze_only:
                    duration, timestamps, black = splitter.detect(video, self.threshold)
                    start, end = (
                        content_range(black, duration)
                        if self.trim_black else (0.0, duration)
                    )
                    boundaries = make_boundaries(
                        timestamps, duration, self.min_duration, start, end, black
                    )
                    scenes = [
                        {"start": a, "end": b, "duration": b - a}
                        for a, b in scene_ranges(boundaries, black)
                    ]
                    self.signals.preview.emit(str(video), scenes)
                    self.signals.log.emit(f"Найдено сцен: {len(scenes)}")
                else:
                    out = self.output_dir(video, self.output_root, len(self.paths) > 1)
                    scenes = splitter.split(
                        video,
                        out,
                        self.threshold,
                        self.min_duration,
                        self.copy_streams,
                        self.overwrite,
                        self.trim_black,
                        log=self.signals.log.emit,
                    )
                    self.signals.log.emit(f"Готово: {len(scenes)} сцен -> {out}")
                self.signals.progress.emit(index, len(self.paths))
            self.signals.finished.emit()
        except (FFmpegError, OSError, ValueError) as error:
            self.signals.failed.emit(str(error))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("InterviewKit — нарезка по сценам")
        self.resize(1000, 720)
        self.worker: SceneWorker | None = None
        self._build_ui()

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)

        top = QHBoxLayout()
        title = QLabel("Нарезка по сценам")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        top.addWidget(title)
        top.addStretch(1)
        self.btn_add_files = QPushButton("Добавить видео…")
        self.btn_add_files.clicked.connect(self._add_files)
        self.btn_add_folder = QPushButton("Добавить папку…")
        self.btn_add_folder.clicked.connect(self._add_folder)
        self.chk_recursive = QCheckBox("Включая вложенные папки")
        self.chk_recursive.setToolTip("Искать видео во вложенных каталогах при добавлении папки")
        top.addWidget(self.chk_recursive)
        top.addWidget(self.btn_add_files)
        top.addWidget(self.btn_add_folder)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.file_list.setAcceptDrops(True)
        self.file_list.setDragDropMode(QListWidget.DragDropMode.DropOnly)
        self.file_list.dragEnterEvent = self._drag_enter
        self.file_list.dropEvent = self._drop_files
        splitter.addWidget(self.file_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        form = QFormLayout()
        self.spin_threshold = QDoubleSpinBox()
        self.spin_threshold.setRange(0.05, 0.95)
        self.spin_threshold.setSingleStep(0.05)
        self.spin_threshold.setDecimals(2)
        self.spin_threshold.setValue(0.35)
        form.addRow("Чувствительность:", self.spin_threshold)

        self.spin_min_duration = QDoubleSpinBox()
        self.spin_min_duration.setRange(0, 600)
        self.spin_min_duration.setSingleStep(0.5)
        self.spin_min_duration.setSuffix(" сек")
        self.spin_min_duration.setValue(1.0)
        form.addRow("Минимальная сцена:", self.spin_min_duration)

        output_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("По умолчанию: рядом с каждым видео")
        self.btn_output = QPushButton("Обзор…")
        self.btn_output.clicked.connect(self._choose_output)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.btn_output)
        form.addRow("Папка результата:", output_row)
        right_layout.addLayout(form)

        self.chk_copy = QCheckBox("Быстро, без перекодирования")
        self.chk_copy.setToolTip(
            "Быстрее, но границы фрагментов могут смещаться к ключевым кадрам"
        )
        self.chk_trim_black = QCheckBox("Удалять чёрный экран в начале и конце")
        self.chk_trim_black.setChecked(True)
        self.chk_trim_black.setToolTip(
            "Чёрные переходы внутри ролика сохраняются"
        )
        self.chk_overwrite = QCheckBox("Перезаписывать существующие результаты")
        right_layout.addWidget(self.chk_copy)
        right_layout.addWidget(self.chk_trim_black)
        right_layout.addWidget(self.chk_overwrite)
        right_layout.addStretch(1)
        splitter.addWidget(right)
        splitter.setSizes([550, 450])
        layout.addWidget(splitter, 2)

        controls = QHBoxLayout()
        self.btn_remove = QPushButton("Убрать выбранные")
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear = QPushButton("Очистить список")
        self.btn_clear.clicked.connect(self.file_list.clear)
        self.btn_analyze = QPushButton("Анализировать")
        self.btn_analyze.clicked.connect(lambda: self._start(analyze_only=True))
        self.btn_split = QPushButton("Нарезать видео")
        self.btn_split.setDefault(True)
        self.btn_split.clicked.connect(lambda: self._start(analyze_only=False))
        controls.addWidget(self.btn_remove)
        controls.addWidget(self.btn_clear)
        controls.addStretch(1)
        controls.addWidget(self.btn_analyze)
        controls.addWidget(self.btn_split)
        layout.addLayout(controls)

        self.preview = QListWidget()
        self.preview.setMinimumHeight(150)
        self.preview.setAlternatingRowColors(True)
        layout.addWidget(QLabel("Сцены / журнал"))
        layout.addWidget(self.preview, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.setStyleSheet(
            "QMainWindow { background: #f5f6f7; }"
            "QPushButton { min-height: 28px; padding: 2px 10px; }"
            "QDoubleSpinBox, QLineEdit, QListWidget { background: white; }"
        )

    def _add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выбрать видео",
            str(Path.cwd()),
            "Видео (*.mp4 *.mov *.mkv *.avi *.webm *.m4v)",
        )
        if files:
            self._add_paths([Path(item) for item in files])

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выбрать папку с видео")
        if folder:
            self._add_paths(collect_videos([Path(folder)], self.chk_recursive.isChecked()))

    def _add_paths(self, paths: list[Path]):
        existing = {self.file_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.file_list.count())}
        added = 0
        for path in paths:
            resolved = path.resolve()
            if resolved.suffix.lower() not in VIDEO_EXTS or str(resolved) in existing:
                continue
            item = QListWidgetItem(resolved.name)
            item.setToolTip(str(resolved))
            item.setData(Qt.ItemDataRole.UserRole, str(resolved))
            self.file_list.addItem(item)
            existing.add(str(resolved))
            added += 1
        if added:
            self.preview.addItem(f"Добавлено файлов: {added}")

    @staticmethod
    def _drag_enter(event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def _drop_files(self, event):
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()]
        self._add_paths(collect_videos(paths, self.chk_recursive.isChecked()))
        event.acceptProposedAction()

    def _remove_selected(self):
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))

    def _choose_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Папка для результата")
        if folder:
            self.output_edit.setText(folder)

    def _start(self, analyze_only: bool):
        if self.worker and self.worker.isRunning():
            return
        paths = [
            Path(self.file_list.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(self.file_list.count())
        ]
        paths = [path for path in paths if path.is_file()]
        if not paths:
            QMessageBox.warning(self, "Нет видео", "Добавьте видеофайлы или папку.")
            return

        output_text = self.output_edit.text().strip()
        output_root = Path(output_text) if output_text else None
        if output_root is not None:
            root_resolved = output_root.resolve()
            if any(
                root_resolved == path.resolve()
                or root_resolved in path.resolve().parents
                for path in paths
            ):
                QMessageBox.warning(
                    self,
                    "Папка результата",
                    "Выберите папку результата вне папки с исходным видео.",
                )
                return
        self.preview.clear()
        self.progress.setRange(0, len(paths))
        self.progress.setValue(0)
        self._set_busy(True)

        self.worker = SceneWorker(
            paths,
            output_root,
            self.spin_threshold.value(),
            self.spin_min_duration.value(),
            self.chk_copy.isChecked(),
            self.chk_trim_black.isChecked(),
            analyze_only,
            self.chk_overwrite.isChecked(),
        )
        self.worker.signals.log.connect(self.preview.addItem)
        self.worker.signals.progress.connect(self._on_progress)
        self.worker.signals.preview.connect(self._on_preview)
        self.worker.signals.finished.connect(self._on_finished)
        self.worker.signals.failed.connect(self._on_failed)
        self.worker.start()

    def _on_preview(self, video: str, scenes: list):
        self.preview.addItem(f"Сцены для {Path(video).name}:")
        for index, scene in enumerate(scenes, 1):
            start, end = scene["start"], scene["end"]
            self.preview.addItem(
                f"  {index:04d}  {self._format_time(start)} - "
                f"{self._format_time(end)}  ({scene['duration']:.2f} сек)"
            )

    @staticmethod
    def _format_time(seconds: float) -> str:
        total = int(seconds)
        return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"

    def _on_progress(self, current: int, total: int):
        self.progress.setValue(current)

    def _on_finished(self):
        self._set_busy(False)
        self.preview.addItem("Готово")

    def _on_failed(self, message: str):
        self._set_busy(False)
        self.preview.addItem(f"Ошибка: {message}")
        QMessageBox.critical(self, "Ошибка обработки", message)

    def _set_busy(self, busy: bool):
        for widget in (
            self.btn_add_files, self.btn_add_folder, self.btn_remove,
            self.btn_clear, self.btn_analyze, self.btn_split, self.btn_output,
            self.spin_threshold, self.spin_min_duration, self.output_edit,
            self.chk_copy, self.chk_trim_black, self.chk_overwrite,
            self.chk_recursive, self.file_list,
        ):
            widget.setEnabled(not busy)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("InterviewKit Scene Splitter")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
