import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import Config
from core.editor import EditItem, Editor
from core.ffmpeg_wrapper import FFmpeg, FFmpegError
from core.llm import (
    LLMError, PolzaClient, SegmentDecision, decide_segments, extract_json_value,
    parse_decisions,
)
from core.scene_splitter import (
    collect_videos, content_range, make_boundaries, merge_black_intervals,
    parse_black_intervals, parse_scene_timestamps, scene_ranges,
)
from core.stt import TranscriptSegment


class FFmpegTests(unittest.TestCase):
    def test_probe_duration_accepts_probe_exit_code(self):
        ffmpeg = FFmpeg()
        log = "Duration: 01:02:03.50, start: 0.000000, bitrate: 1 kb/s"
        with patch.object(ffmpeg, "_run", return_value=log) as run:
            self.assertEqual(ffmpeg.probe_duration("input.mp4"), 3723.5)
            self.assertFalse(run.call_args.kwargs["check"])

    def test_probe_duration_rejects_unknown_output(self):
        ffmpeg = FFmpeg()
        with patch.object(ffmpeg, "_run", return_value="no duration"):
            with self.assertRaises(FFmpegError):
                ffmpeg.probe_duration("input.mp4")

    def test_silent_clip_gets_generated_audio(self):
        ffmpeg = FFmpeg()
        with patch.object(ffmpeg, "has_audio", return_value=False), patch.object(
            ffmpeg, "_run", return_value=""
        ) as run:
            ffmpeg.normalize_clip("in.mp4", "out.mp4", 1280, 720, 30, 48000, 2)
        args = run.call_args.args[0]
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000", args)
        self.assertEqual(args.count("-i"), 2)

    def test_detects_audio_stream_with_language_metadata(self):
        ffmpeg = FFmpeg()
        log = "Stream #0:1(und): Audio: aac, 48000 Hz, stereo"
        with patch.object(ffmpeg, "_run", return_value=log):
            self.assertTrue(ffmpeg.has_audio("input.mp4"))

    def test_cut_silent_video_gets_generated_audio(self):
        ffmpeg = FFmpeg()
        with patch.object(ffmpeg, "has_audio", return_value=False), patch.object(
            ffmpeg, "_run", return_value=""
        ) as run:
            ffmpeg.cut_and_normalize("in.mp4", 0, 2, "out.mp4", 1280, 720, 30, 48000, 2)
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000", run.call_args.args[0])


class EditorTests(unittest.TestCase):
    def test_build_full_plan_keeps_all_segments_without_interruptions(self):
        editor = Editor(ffmpeg=None)
        transcript = [TranscriptSegment(0, 1.0, 3.0, "Про любовь")]
        plan = editor.build_full_plan("raw.mp4", transcript)
        self.assertEqual([item.kind for item in plan], ["segment"])
        self.assertEqual(plan[0].start, 1.0)

    def test_edit_item_defaults_for_old_or_partial_plan(self):
        item = EditItem.from_dict({"kind": "segment", "source_path": "raw.mp4"})
        self.assertEqual(item.start, 0.0)
        self.assertEqual(item.end, 0.0)

    def test_edit_item_ignores_legacy_categories_field(self):
        # Старые планы содержали categories — они не должны ломать разбор.
        item = EditItem.from_dict(
            {"kind": "segment", "source_path": "raw.mp4", "categories": ["love"]}
        )
        self.assertEqual(item.kind, "segment")


class LLMTests(unittest.TestCase):
    def test_extract_json_from_fenced_block(self):
        text = 'Вот ответ:\n```json\n[{"index":0,"keep":true}]\n```\nГотово.'
        self.assertEqual(extract_json_value(text), [{"index": 0, "keep": True}])

    def test_parse_decisions_drops_unknown_interruption(self):
        text = '[{"index":1,"keep":true,"interruption":"такой_нет.mp4"}]'
        decisions = parse_decisions(text, valid_interruptions=["love.mp4"])
        self.assertEqual(decisions[0].interruption, "")

    def test_parse_decisions_accepts_known_interruption(self):
        text = '[{"index":1,"keep":true,"interruption":"love.mp4"}]'
        decisions = parse_decisions(text, valid_interruptions=["love.mp4"])
        self.assertEqual(decisions[0].interruption, "love.mp4")

    def test_parse_decisions_normalizes_path_to_basename(self):
        # Модель может вернуть путь — сверяем и запоминаем по имени файла.
        text = '[{"index":1,"keep":true,"interruption":"clips/love.mp4"}]'
        decisions = parse_decisions(text, valid_interruptions=["love.mp4"])
        self.assertEqual(decisions[0].interruption, "love.mp4")

    def test_parse_decisions_matches_basename_against_full_paths(self):
        # Список допустимых может содержать полные пути, а модель возвращает
        # имя файла — сравнение идёт по basename, чтобы перебивка не терялась.
        text = '[{"index":0,"keep":true,"interruption":"gorko.mp4"}]'
        valid = [r"C:\clips\gorko.mp4", r"C:\clips\romantika.mp4"]
        decisions = parse_decisions(text, valid_interruptions=valid)
        self.assertEqual(decisions[0].interruption, "gorko.mp4")

    def test_parse_decisions_defaults_keep_true(self):
        text = '[{"index":0}]'
        decisions = parse_decisions(text, valid_interruptions=[])
        self.assertTrue(decisions[0].keep)

    def test_parse_decisions_accepts_object_wrapper(self):
        text = '{"decisions":[{"index":2,"keep":false}]}'
        decisions = parse_decisions(text, valid_interruptions=[])
        self.assertEqual(decisions[0].index, 2)
        self.assertFalse(decisions[0].keep)

    def test_extract_json_raises_without_json(self):
        with self.assertRaises(LLMError):
            extract_json_value("тут нет ничего похожего")

    def test_build_plan_llm_cuts_and_inserts_by_filename(self):
        editor = Editor(ffmpeg=None)
        transcript = [
            TranscriptSegment(0, 1.0, 3.0, "Про любовь"),
            TranscriptSegment(1, 3.0, 5.0, "эээ ну это заполнитель"),
        ]
        decisions = [
            SegmentDecision(index=0, keep=True, interruption="clip.mp4"),
            SegmentDecision(index=1, keep=False, reason="заполнитель"),
        ]
        with patch.object(editor, "interruption_by_name", return_value="/x/clip.mp4"):
            plan = editor.build_edit_plan_llm(decisions, "raw.mp4", transcript)
        # Сегмент 1 вырезан, после сегмента 0 вставлена перебивка clip.mp4.
        self.assertEqual([item.kind for item in plan], ["segment", "interruption"])
        self.assertEqual(plan[1].source_path, "/x/clip.mp4")

    def test_build_plan_llm_keeps_segment_without_decision(self):
        # Сегмент, про который LLM не высказалась, остаётся (не теряем контент).
        editor = Editor(ffmpeg=None)
        transcript = [TranscriptSegment(0, 0.0, 2.0, "текст")]
        plan = editor.build_edit_plan_llm([], "raw.mp4", transcript)
        self.assertEqual([item.kind for item in plan], ["segment"])


class FakeClient:
    """Подменный клиент LLM: возвращает заранее заданный ответ."""

    def __init__(self, reply: str):
        self.reply = reply
        self.last_messages = None

    def chat(self, messages, temperature=0.2, max_tokens=None):
        self.last_messages = messages
        return self.reply


class DecideSegmentsTests(unittest.TestCase):
    def test_decide_segments_builds_prompt_and_parses(self):
        transcript = [TranscriptSegment(0, 1.0, 3.0, "про любовь")]
        client = FakeClient('[{"index":0,"keep":true}]')
        decisions = decide_segments(client, transcript, ["love"])
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0].keep)
        # Системный и пользовательский промпт ушли в модель.
        self.assertEqual(len(client.last_messages), 2)
        self.assertIn("про любовь", client.last_messages[1]["content"])

    def test_decide_segments_empty_transcript_skips_llm(self):
        client = FakeClient("[]")
        self.assertEqual(decide_segments(client, [], ["love"]), [])
        self.assertIsNone(client.last_messages)


class ConfigTests(unittest.TestCase):
    def test_resolution_validation(self):
        self.assertEqual(Config(output_resolution="1280x720").resolution_wh(), (1280, 720))
        with self.assertRaisesRegex(ValueError, "ожидается формат"):
            Config(output_resolution="bad").resolution_wh()


class SceneSplitterTests(unittest.TestCase):
    def test_parses_unique_scene_timestamps(self):
        log = "pts_time:2.5 foo\npts_time:7.25 bar\npts_time:2.5"
        self.assertEqual(parse_scene_timestamps(log), [2.5, 7.25])

    def test_boundaries_filter_short_scenes_and_tail(self):
        self.assertEqual(
            make_boundaries([0.2, 2.0, 2.4, 9.5], duration=10.0, min_scene_duration=1.0),
            [0.0, 2.0, 10.0],
        )

    def test_collect_videos_filters_extensions(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.mp4").touch()
            (root / "b.txt").touch()
            self.assertEqual(collect_videos([root]), [(root / "a.mp4").resolve()])

    def test_recognizes_windows_access_violation(self):
        from core.scene_splitter import SceneSplitter
        error = FFmpegError("crash", returncode=3221225477)
        self.assertTrue(SceneSplitter._is_access_violation(error))

    def test_recognizes_crash_from_stderr_text(self):
        # Некоторые сборки ловят сигнал, завершаются с другим кодом, но пишут
        # текст падения в stderr — это тоже авария, нужен повтор.
        from core.scene_splitter import SceneSplitter
        for text in (
            "ffmpeg has crashed with signal Segmentation fault",
            "[h264 @ 000] Exception Access Violation - terminating",
        ):
            self.assertTrue(SceneSplitter._is_access_violation(FFmpegError(text, returncode=1)))

    def test_ignores_normal_ffmpeg_error(self):
        from core.scene_splitter import SceneSplitter
        error = FFmpegError("No such file or directory", returncode=1)
        self.assertFalse(SceneSplitter._is_access_violation(error))

    def test_retries_in_single_thread_on_crash(self):
        # Первый запуск падает с ACCESS_VIOLATION, повтор в однопоточном режиме
        # успешен -> результат возвращается, а не выбрасывается ошибка.
        from core.scene_splitter import SceneSplitter

        class FlakyFFmpeg:
            def __init__(self):
                self.calls = []

            def _run(self, args):
                self.calls.append(args)
                if len(self.calls) == 1:
                    raise FFmpegError("crash", returncode=3221225477)
                return "ok"

        flaky = FlakyFFmpeg()
        splitter = SceneSplitter(flaky)
        logs = []
        result = splitter._run_with_crash_retry(
            ["-i", "in"], ["-threads", "1", "-i", "in"], logs.append, "тест"
        )
        self.assertEqual(result, "ok")
        self.assertEqual(len(flaky.calls), 2)
        self.assertIn("-threads", flaky.calls[1])
        self.assertTrue(any("повтор 1/3" in line for line in logs))

    def test_raises_clear_error_when_all_retries_crash(self):
        from core.scene_splitter import SceneSplitter

        class AlwaysCrashFFmpeg:
            def __init__(self):
                self.calls = 0

            def _run(self, args):
                self.calls += 1
                raise FFmpegError("crash", returncode=3221225477)

        splitter = SceneSplitter(AlwaysCrashFFmpeg())
        with self.assertRaises(FFmpegError) as ctx:
            splitter._run_with_crash_retry(["a"], ["b"], lambda _: None, "тест")
        # 1 обычный запуск + 3 однопоточных повтора.
        self.assertIn("--copy", str(ctx.exception))
        self.assertEqual(ctx.exception.returncode, 3221225477)

    def test_on_crash_callback_runs_before_each_retry(self):
        from core.scene_splitter import SceneSplitter

        class CrashTwiceFFmpeg:
            def __init__(self):
                self.calls = 0

            def _run(self, args):
                self.calls += 1
                if self.calls <= 2:
                    raise FFmpegError("crash", returncode=3221225477)
                return "ok"

        cleaned = []
        splitter = SceneSplitter(CrashTwiceFFmpeg())
        result = splitter._run_with_crash_retry(
            ["a"], ["b"], lambda _: None, "тест",
            on_crash=lambda: cleaned.append(1),
        )
        self.assertEqual(result, "ok")
        # Удаление битого выходного файла — после каждого аварийного падения.
        self.assertEqual(len(cleaned), 2)

    def test_parses_black_intervals_and_trims_only_edges(self):
        log = (
            "black_start:0 black_end:1.5 black_duration:1.5\n"
            "black_start:4 black_end:5 black_duration:1\n"
            "black_start:9 black_end:10 black_duration:1"
        )
        intervals = parse_black_intervals(log)
        self.assertEqual(intervals, [(0.0, 1.5), (4.0, 5.0), (9.0, 10.0)])
        self.assertEqual(content_range(intervals, 10.0), (1.5, 9.0))

    def test_boundaries_use_trimmed_content_range(self):
        self.assertEqual(
            make_boundaries([1.5, 4.0, 9.0], 10.0, 1.0, 1.5, 9.0),
            [1.5, 4.0, 9.0],
        )

    def test_merges_split_black_transition_into_one_interval(self):
        # Фильтр разрезал один длинный чёрный переход на куски с разрывом 0.1 с.
        self.assertEqual(
            merge_black_intervals([(293.4, 293.6), (293.7, 294.567)]),
            [(293.4, 294.567)],
        )

    def test_keeps_distant_black_intervals_separate(self):
        self.assertEqual(
            merge_black_intervals([(1.0, 2.0), (5.0, 6.0)]),
            [(1.0, 2.0), (5.0, 6.0)],
        )

    def test_merged_black_pushes_boundary_past_whole_transition(self):
        # Граница 293.5 стоит внутри разрезанного чёрного -> после слияния
        # она уходит за конец всего перехода 294.567.
        blacks = [(293.4, 293.6), (293.7, 294.567)]
        self.assertEqual(
            make_boundaries([293.5], 300.0, 1.0, 0.0, 300.0, blacks),
            [0.0, 294.567, 300.0],
        )

    def test_merged_black_trims_trailing_transition_fully(self):
        # Хвост сцены обрезается по началу всего слившегося перехода, а не по
        # последнему куску — иначе в сцене останется начало чёрного.
        blacks = [(293.4, 293.6), (293.7, 294.567)]
        self.assertEqual(
            scene_ranges([0.0, 294.567, 300.0], blacks),
            [(0.0, 293.4), (294.567, 300.0)],
        )

    def test_boundaries_shift_past_black_so_scene_starts_on_content(self):
        # Граница 5.0 попала в чёрный переход [5.0, 5.5] -> сдвигаем за его конец.
        # Граница 2.0 стоит на контенте и не меняется.
        self.assertEqual(
            make_boundaries(
                [2.0, 5.0], 10.0, 1.0, 0.0, 10.0, [(5.0, 5.5)],
            ),
            [0.0, 2.0, 5.5, 10.0],
        )

    def test_boundary_just_before_black_also_shifts(self):
        # Граница 4.95 вплотную (<= 0.1) к началу чёрного -> уводим за конец.
        self.assertEqual(
            make_boundaries(
                [4.95], 10.0, 1.0, 0.0, 10.0, [(5.0, 5.5)],
            ),
            [0.0, 5.5, 10.0],
        )

    def test_boundary_shift_respects_min_scene_duration(self):
        # Граница 5.0 сдвигается за чёрный [5.0, 5.5] -> 5.5.
        # Следующая граница 6.0 оказывается всего в 0.5 сек (минимум 1.0) -> отброшена.
        self.assertEqual(
            make_boundaries(
                [5.0, 6.0], 10.0, 1.0, 0.0, 10.0, [(5.0, 5.5)],
            ),
            [0.0, 5.5, 10.0],
        )

    def test_scene_ranges_drop_trailing_black_transition(self):
        # Граница 5.5 стоит на конце чёрного [5.0, 5.5] -> хвост сцены обрезается
        # до 5.0, чёрный переход исключается из обеих сцен.
        self.assertEqual(
            scene_ranges([0.0, 2.0, 5.5, 10.0], [(5.0, 5.5)]),
            [(0.0, 2.0), (2.0, 5.0), (5.5, 10.0)],
        )

    def test_scene_ranges_keeps_boundary_not_at_black(self):
        # Граница 5.0 не совпадает с концом чёрного -> хвост не трогаем.
        self.assertEqual(
            scene_ranges([0.0, 5.0, 10.0], [(2.0, 2.5)]),
            [(0.0, 5.0), (5.0, 10.0)],
        )

    def test_scene_ranges_never_returns_inverted_range(self):
        # Чёрный [9.0, 10.0] начался внутри последней сцены [9.5, 10.0].
        # Обрезка дала бы 9.0 < 9.5 -> хвост сохраняем как есть.
        self.assertEqual(
            scene_ranges([0.0, 9.5, 10.0], [(9.0, 10.0)]),
            [(0.0, 9.5), (9.5, 10.0)],
        )


if __name__ == "__main__":
    unittest.main()
