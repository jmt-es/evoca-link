import ast
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock

import link_export
from link_export import EXPORT_DIR_VARIABLE, Export, answer_from_export, chunk, find_export_root, parse_day, parse_time

PLUS_TWO = timezone(timedelta(hours=2))
BASE = datetime(2026, 10, 9, 9, 0, 0, tzinfo=PLUS_TWO)
NOW = BASE + timedelta(minutes=90)
TODAY = date(2026, 10, 9)
YESTERDAY = date(2026, 10, 8)
WRITTEN_TODAY = "2026-10-09T10:00:00+02:00"
WRITTEN_YESTERDAY = "2026-10-09T00:10:00+02:00"
REASON = "The iPhone is not connected right now."


def stamp(base: datetime, **offset: float) -> str:
    return (base + timedelta(**offset)).isoformat(timespec="seconds")


def busy_day(base: datetime) -> list[dict]:
    def at(**offset: float) -> str:
        return stamp(base, **offset)

    return [
        {"type": "segment", "id": "S1", "start": at(), "end": at(minutes=5), "source": "watch", "transcription": "accurate"},
        {"type": "utterance", "segment": "S1", "start": at(seconds=10), "end": at(seconds=14), "lang": "es", "text": "Vamos a tomar un café con el proveedor"},
        {"type": "utterance", "segment": "S1", "start": at(seconds=60), "end": at(seconds=65), "lang": "es", "speaker": "Tú", "speakerID": "8B0F7C52-6A2B-4D7E-9C55-0E8E5B1C2D3A", "text": "El presupuesto del almacén sale en 4.200 euros"},
        {"type": "utterance", "segment": "S1", "start": at(seconds=61), "end": at(seconds=64), "lang": "es", "text": "Esta receta de pasta se hace en diez minutos", "onScreen": True},
        {"type": "moment", "id": "M1", "at": at(seconds=62), "trigger": "tap", "title": "Presupuesto"},
        {"type": "utterance", "segment": "S1", "start": at(minutes=4), "end": at(minutes=4, seconds=3), "lang": "en", "text": "Please check the shipping label tomorrow"},
        {"type": "utterance", "segment": "S1", "start": at(minutes=4, seconds=30), "end": at(minutes=4, seconds=33), "lang": "es", "speaker": "Persona 1", "text": "Mañana revisamos la entrega de los tornillos"},
        {"type": "place", "start": at(minutes=-30), "end": at(minutes=30), "name": "Casa", "source": "named", "symbol": "house.fill"},
        {"type": "place", "start": at(minutes=30), "end": at(minutes=90), "name": "Lugar 1", "source": "frequent"},
        {"type": "conversation", "start": at(seconds=10), "end": at(minutes=4, seconds=33), "title": "Presupuesto del almacén", "gist": "Se habló del presupuesto y de la entrega.", "points": ["4.200 euros"], "people": ["Persona 1"], "followUps": [{"text": "Enviar el presupuesto", "kind": "task", "at": at(seconds=60)}], "status": "generated", "engine": "AFM 3 Core Advanced · v1"},
        {"type": "recap", "day": base.date().isoformat(), "headline": "Presupuesto del almacén cerrado", "summary": "Por la mañana se cerró el presupuesto.", "dayparts": [{"part": "mañana", "text": "Se cerró el presupuesto."}], "keyMoments": [{"at": at(seconds=62), "text": "Cifra final"}], "followUps": [{"text": "Enviar el presupuesto", "when": "mañana", "kind": "task", "at": at(seconds=60)}], "people": ["Persona 1"], "places": ["Casa"], "coverage": [at(seconds=10), at(minutes=5)], "final": False, "status": "generated", "engine": "AFM 3 Core Advanced · v1", "generatedAt": at(minutes=55)},
    ]


def quiet_day(base: datetime) -> list[dict]:
    def at(**offset: float) -> str:
        return stamp(base, **offset)

    return [
        {"type": "segment", "id": "S0", "start": at(), "end": at(minutes=3), "source": "phone", "transcription": "accurate"},
        {"type": "utterance", "segment": "S0", "start": at(seconds=5), "end": at(seconds=9), "lang": "es", "text": "La reunión con el proveedor empieza a las diez"},
        {"type": "utterance", "segment": "S0", "start": at(minutes=2), "end": at(minutes=2, seconds=4), "lang": "es", "text": "Hay que pedir más cajas para el envío"},
        {"type": "moment", "id": "M0", "at": at(minutes=2, seconds=1), "trigger": "tap", "title": "Cajas", "note": "Pedir más cajas"},
        {"type": "recap", "day": base.date().isoformat(), "headline": "Reunión con el proveedor", "summary": "Se preparó la reunión.", "dayparts": [], "keyMoments": [], "followUps": [], "people": [], "places": [], "coverage": [at(seconds=5), at(minutes=3)], "final": True, "status": "generated", "engine": "AFM 3 Core Advanced · v1", "generatedAt": at(hours=14, minutes=50)},
    ]


def event_day(event: dict) -> str:
    if event["type"] == "recap":
        return event["day"]
    return parse_time(event.get("start") or event["at"]).date().isoformat()


def markdown_of(day: str, events: list[dict]) -> str:
    lines = [f"# {day}", ""]
    for event in events:
        if event["type"] == "utterance":
            marker = "_(en pantalla)_ " if event.get("onScreen") else ""
            lines += [f"**{parse_time(event['start']).strftime('%H:%M:%S')}** {marker}{event['text']}", ""]
    return "\n".join(lines) + "\n"


def write_export(root: Path, base: datetime = BASE, exported_at: datetime | None = None) -> Path:
    """Two days of invented content under root/Evoca: the busy one at base and the quiet one a day earlier, each in the folder of its own date."""
    export = root / "Evoca"
    by_day: dict[str, list[dict]] = {}
    for event in busy_day(base) + quiet_day(base - timedelta(days=1)):
        by_day.setdefault(event_day(event), []).append(event)
    newest = max(by_day)
    written = {}
    for day, events in by_day.items():
        folder = export / day
        folder.mkdir(parents=True)
        (folder / f"{day}.jsonl").write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
        (folder / f"{day}.md").write_text(markdown_of(day, events), encoding="utf-8")
        following = datetime.combine(date.fromisoformat(day) + timedelta(days=1), time(0, 10), tzinfo=base.tzinfo)
        written[day] = (exported_at or base + timedelta(hours=1)) if day == newest else following
    manifest = {"format": "evoca.export/1", "generator": "Evoca 1.3", "days": {day: when.isoformat(timespec="seconds") for day, when in written.items()}}
    (export / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return export


def write_manifest(folder: Path, format_name: str = "evoca.export/1") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps({"format": format_name, "generator": "Evoca 1.3", "days": {}}), encoding="utf-8")


def recording_export(reads: list[str]) -> type[Export]:
    class Recording(Export):
        def read(self, path: Path) -> str | None:
            reads.append(f"{path.parent.name}/{path.name}")
            return super().read(path)

    return Recording


class ExportFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = write_export(Path(self.temp.name))
        self.export = Export(self.root, NOW)

    def answer(self, method: str, params: dict | None = None, now: datetime = NOW) -> dict:
        return answer_from_export(self.root, method, params or {}, REASON, now)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        environment = mock.patch.dict(os.environ, {"HOME": str(self.base / "home"), "USERPROFILE": str(self.base / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(EXPORT_DIR_VARIABLE, None)

    def test_an_explicit_folder_with_a_manifest_is_the_export(self):
        write_manifest(self.base / "chosen")
        self.assertEqual(find_export_root(str(self.base / "chosen")), self.base / "chosen")

    def test_the_folder_that_holds_the_evoca_folder_works_too(self):
        write_manifest(self.base / "chosen" / "Evoca")
        self.assertEqual(find_export_root(str(self.base / "chosen")), self.base / "chosen" / "Evoca")

    def test_the_environment_variable_points_to_the_export(self):
        write_manifest(self.base / "elsewhere" / "Evoca")
        os.environ[EXPORT_DIR_VARIABLE] = str(self.base / "elsewhere")
        self.assertEqual(find_export_root(), self.base / "elsewhere" / "Evoca")

    def test_an_explicit_path_wins_over_the_environment_variable(self):
        write_manifest(self.base / "explicit")
        write_manifest(self.base / "variable")
        os.environ[EXPORT_DIR_VARIABLE] = str(self.base / "variable")
        self.assertEqual(find_export_root(str(self.base / "explicit")), self.base / "explicit")

    def test_a_configured_folder_without_an_export_is_never_replaced_by_another(self):
        write_manifest(self.base / "home" / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Evoca")
        os.environ[EXPORT_DIR_VARIABLE] = str(self.base / "empty")
        with self.assertRaisesRegex(FileNotFoundError, "no manifest.json in .*empty"):
            find_export_root()

    def test_icloud_drive_and_cloud_storage_are_searched_without_configuration(self):
        icloud = self.base / "home" / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Notes" / "Evoca"
        write_manifest(icloud)
        self.assertEqual(find_export_root(), icloud)
        for manifest in icloud.glob("manifest.json"):
            manifest.unlink()
        drive = self.base / "home" / "Library" / "CloudStorage" / "GoogleDrive-someone" / "My Drive" / "Evoca"
        write_manifest(drive)
        self.assertEqual(find_export_root(), drive)

    def test_the_search_goes_four_folders_down_and_no_further(self):
        provider = self.base / "provider"
        write_manifest(provider / "a" / "b" / "c" / "Evoca")
        self.assertEqual(find_export_root(providers=[provider]), provider / "a" / "b" / "c" / "Evoca")
        deeper = self.base / "deeper"
        write_manifest(deeper / "a" / "b" / "c" / "d" / "Evoca")
        with self.assertRaises(FileNotFoundError):
            find_export_root(providers=[deeper])

    def test_other_formats_hidden_folders_and_broken_manifests_are_skipped(self):
        provider = self.base / "provider"
        write_manifest(provider / "Other", "evoca.export/9")
        write_manifest(provider / ".hidden")
        (provider / "Broken").mkdir(parents=True)
        (provider / "Broken" / "manifest.json").write_text("[1, 2", encoding="utf-8")
        (provider / "List").mkdir(parents=True)
        (provider / "List" / "manifest.json").write_text("[]", encoding="utf-8")
        write_manifest(provider / "Zeta")
        self.assertEqual(find_export_root(providers=[provider]), provider / "Zeta")

    def test_a_folder_that_cannot_be_read_is_skipped_instead_of_failing_the_search(self):
        locked = self.base / "locked"
        locked.mkdir()
        write_manifest(self.base / "open" / "Evoca")
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)
        self.assertEqual(find_export_root(providers=[locked, self.base / "open"]), self.base / "open" / "Evoca")
        with self.assertRaisesRegex(FileNotFoundError, "no manifest.json in"):
            find_export_root(str(locked))

    def test_nothing_found_says_where_it_looked(self):
        with self.assertRaisesRegex(FileNotFoundError, "iCloud Drive or ~/Library/CloudStorage"):
            find_export_root()
        self.assertIn(EXPORT_DIR_VARIABLE, link_export.HOW_TO_POINT)


class ListDaysTests(ExportFixture):
    def test_days_come_newest_first_with_the_numbers_the_live_tool_gives(self):
        self.assertEqual(self.export.list_days(14), {"days": [
            {"date": "2026-10-09", "recorded_minutes": 5, "phrases": 4, "moments": 1, "headline": "Presupuesto del almacén cerrado", "places": ["Casa"]},
            {"date": "2026-10-08", "recorded_minutes": 3, "phrases": 2, "moments": 1, "headline": "Reunión con el proveedor", "places": []},
        ]})

    def test_the_limit_counts_days_and_keeps_the_newest(self):
        self.assertEqual([row["date"] for row in self.export.list_days(1)["days"]], ["2026-10-09"])

    def test_what_sounded_on_screen_and_unnamed_places_stay_out_of_the_counts(self):
        row = self.export.list_days(1)["days"][0]
        self.assertEqual(row["phrases"], 4)
        self.assertEqual(row["places"], ["Casa"])


class ReadDayTests(ExportFixture):
    def test_the_markdown_of_the_day_comes_whole_when_it_fits(self):
        text = (self.root / "2026-10-09" / "2026-10-09.md").read_text(encoding="utf-8")
        result = self.export.read_day(TODAY, 0)
        self.assertEqual(result, {"date": "2026-10-09", "total_chars": len(text), "offset": 0, "next_offset": None, "markdown": text})
        self.assertIn("**09:01:01** _(en pantalla)_ Esta receta de pasta", result["markdown"])

    def test_a_long_day_comes_in_chunks_that_end_on_a_line_break(self):
        lines = [f"**{index:05d}** " + "x" * 90 for index in range(1_200)]
        long_text = "\n".join(lines) + "\n"
        (self.root / "2026-10-08" / "2026-10-08.md").write_text(long_text, encoding="utf-8")
        pieces, offset = [], 0
        while offset is not None:
            result = self.export.read_day(YESTERDAY, offset)
            self.assertLessEqual(len(result["markdown"]), link_export.DAY_CHUNK_CHARS)
            self.assertEqual(result["total_chars"], len(long_text))
            self.assertEqual(result["offset"], offset)
            self.assertTrue(result["markdown"].endswith("\n") or result["next_offset"] is None)
            pieces.append(result["markdown"])
            offset = result["next_offset"]
        self.assertGreater(len(pieces), 2)
        self.assertEqual("".join(pieces), long_text)

    def test_chunk_reports_the_next_offset_and_clamps_the_offset(self):
        text = "uno\ndos\ntres\n"
        first, next_offset = chunk(text, 0, 9)
        self.assertEqual((first, next_offset), ("uno\ndos\n", 8))
        self.assertEqual(chunk(text, next_offset, 100), ("tres\n", None))
        self.assertEqual(chunk(text, -5, 100), (text, None))
        self.assertEqual(chunk(text, 500, 100), ("", None))
        self.assertEqual(chunk("sin saltos de línea", 0, 5), ("sin s", 5))

    def test_a_day_that_was_not_exported_is_an_honest_empty_result(self):
        self.assertEqual(self.export.read_day(date(2026, 10, 1), 0), {"date": "2026-10-01", "total_chars": 0, "offset": 0, "next_offset": None, "markdown": ""})

    def test_a_negative_offset_reads_from_the_start(self):
        self.assertEqual(self.export.read_day(TODAY, -40)["offset"], 0)


class SearchTests(ExportFixture):
    def test_accents_and_case_do_not_matter(self):
        self.assertEqual([hit["text"] for hit in self.export.search("CAFE", None, None, 20, False)["hits"]], ["Vamos a tomar un café con el proveedor"])
        self.assertEqual([hit["text"] for hit in self.export.search("manana", None, None, 20, False)["hits"]], ["Mañana revisamos la entrega de los tornillos"])
        self.assertEqual([hit["text"] for hit in self.export.search("ALMACÉN", None, None, 20, False)["hits"]], ["El presupuesto del almacén sale en 4.200 euros"])

    def test_phrases_with_every_word_come_first_and_the_rest_are_left_out_when_one_has_them_all(self):
        both = self.export.search("cafe proveedor", None, None, 20, False)
        self.assertEqual([hit["text"] for hit in both["hits"]], ["Vamos a tomar un café con el proveedor"])
        either = self.export.search("cafe tornillos", None, None, 20, False)
        self.assertEqual(len(either["hits"]), 2)

    def test_ties_go_newest_first_across_days(self):
        hits = self.export.search("proveedor", None, None, 20, False)["hits"]
        self.assertEqual([(hit["date"], hit["time"]) for hit in hits], [("2026-10-09", "09:00:10"), ("2026-10-08", "09:00:05")])

    def test_a_hit_has_the_shape_of_the_live_one_and_says_it_matched_words(self):
        hit = self.export.search("presupuesto", None, None, 20, False)["hits"][0]
        self.assertEqual(hit, {
            "date": "2026-10-09",
            "time": "09:01:00",
            "speaker": "Tú",
            "place": "Casa",
            "text": "El presupuesto del almacén sale en 4.200 euros",
            "before": "Vamos a tomar un café con el proveedor",
            "after": "Esta receta de pasta se hace en diez minutos",
            "match": "words",
            "on_screen": False,
        })
        self.assertEqual(self.export.search("  presupuesto  ", None, None, 20, False)["query"], "presupuesto")

    def test_what_sounded_on_screen_stays_out_unless_asked(self):
        self.assertEqual(self.export.search("receta pasta", None, None, 20, False)["hits"], [])
        hit = self.export.search("receta pasta", None, None, 20, True)["hits"][0]
        self.assertTrue(hit["on_screen"])
        self.assertEqual(hit["time"], "09:01:01")

    def test_since_until_and_the_limit_narrow_the_search(self):
        self.assertEqual([hit["date"] for hit in self.export.search("proveedor", YESTERDAY, YESTERDAY, 20, False)["hits"]], ["2026-10-08"])
        self.assertEqual([hit["date"] for hit in self.export.search("proveedor", TODAY, None, 20, False)["hits"]], ["2026-10-09"])
        self.assertEqual(len(self.export.search("proveedor", None, None, 1, False)["hits"]), 1)
        self.assertEqual(self.export.search("proveedor", date(2026, 11, 1), None, 20, False)["hits"], [])

    def test_a_query_of_short_words_still_searches(self):
        texts = [hit["text"] for hit in self.export.search("de", None, None, 20, False)["hits"]]
        self.assertEqual(texts, ["Mañana revisamos la entrega de los tornillos", "El presupuesto del almacén sale en 4.200 euros"])

    def test_a_blank_query_finds_nothing_instead_of_everything(self):
        self.assertEqual(self.export.search("   ", None, None, 20, False)["hits"], [])


class MomentTests(ExportFixture):
    def test_a_moment_has_its_place_and_what_was_said_around_it_without_the_screen(self):
        moments = self.export.moments(TODAY, TODAY)
        self.assertEqual(moments, {"moments": [{
            "date": "2026-10-09",
            "time": "09:01:02",
            "title": "Presupuesto",
            "note": None,
            "place": "Casa",
            "said_around": "El presupuesto del almacén sale en 4.200 euros",
        }], "truncated": False})

    def test_moments_come_in_time_order_across_days(self):
        rows = self.export.moments(None, None)["moments"]
        self.assertEqual([(row["date"], row["title"], row["note"]) for row in rows], [("2026-10-08", "Cajas", "Pedir más cajas"), ("2026-10-09", "Presupuesto", None)])
        self.assertEqual(rows[0]["place"], None)

    def test_only_the_latest_moments_are_kept_and_the_result_says_so(self):
        with mock.patch.object(link_export, "MAX_MOMENTS", 1):
            result = self.export.moments(None, None)
        self.assertEqual([row["title"] for row in result["moments"]], ["Presupuesto"])
        self.assertTrue(result["truncated"])


class RecentTests(ExportFixture):
    def test_recent_lists_the_phrases_of_the_window_in_order_with_speaker_and_place(self):
        result = self.export.recent(2, False)
        self.assertEqual(result["hours"], 2)
        self.assertFalse(result["truncated"])
        self.assertEqual([(p["time"], p["speaker"], p["place"]) for p in result["phrases"]], [
            ("09:00:10", None, "Casa"),
            ("09:01:00", "Tú", "Casa"),
            ("09:04:00", None, "Casa"),
            ("09:04:30", "Persona 1", "Casa"),
        ])
        self.assertEqual(result["phrases"][0], {"date": "2026-10-09", "time": "09:00:10", "speaker": None, "place": "Casa", "text": "Vamos a tomar un café con el proveedor"})

    def test_the_window_ends_where_it_starts(self):
        self.assertEqual([p["time"] for p in Export(self.root, BASE + timedelta(minutes=2)).recent(1, False)["phrases"]], ["09:00:10", "09:01:00"])
        self.assertEqual(Export(self.root, BASE + timedelta(hours=5)).recent(1, False)["phrases"], [])

    def test_a_long_window_reaches_into_the_day_before(self):
        phrases = self.export.recent(26, False)["phrases"]
        self.assertEqual([(p["date"], p["time"]) for p in phrases], [("2026-10-08", "09:00:05"), ("2026-10-08", "09:02:00"), ("2026-10-09", "09:00:10"), ("2026-10-09", "09:01:00"), ("2026-10-09", "09:04:00"), ("2026-10-09", "09:04:30")])

    def test_what_sounded_on_screen_stays_out_unless_asked(self):
        self.assertEqual(len(self.export.recent(2, False)["phrases"]), 4)
        self.assertEqual(len(self.export.recent(2, True)["phrases"]), 5)

    def test_only_the_latest_phrases_are_kept_and_the_result_says_so(self):
        with mock.patch.object(link_export, "MAX_RECENT_PHRASES", 2):
            result = self.export.recent(2, False)
        self.assertEqual([p["time"] for p in result["phrases"]], ["09:04:00", "09:04:30"])
        self.assertTrue(result["truncated"])

    def test_the_day_folder_of_another_time_zone_still_counts(self):
        early = {"type": "utterance", "start": "2026-10-09T00:30:00+02:00", "end": "2026-10-09T00:30:04+02:00", "text": "Una frase de madrugada"}
        folder = self.root / "2026-10-09"
        with open(folder / "2026-10-09.jsonl", "a", encoding="utf-8") as file:
            file.write(json.dumps(early) + "\n")
        evening = Export(self.root, datetime(2026, 10, 8, 19, 0, tzinfo=timezone(timedelta(hours=-4))))
        phrases = evening.recent(1, False)["phrases"]
        self.assertEqual([(p["date"], p["time"], p["text"]) for p in phrases], [("2026-10-09", "00:30:00", "Una frase de madrugada")])


class TranscriptSinceTests(ExportFixture):
    def test_the_window_lists_phrases_and_never_claims_to_be_complete(self):
        result = self.export.transcript_since(90, False)
        self.assertEqual(result["window"], {"start": "2026-10-09T09:00:00+02:00", "end": "2026-10-09T10:30:00+02:00", "minutes": 90})
        self.assertEqual([(p["time"], p["end"], p["speaker"]) for p in result["phrases"]], [
            ("09:00:10", "09:00:14", None),
            ("09:01:00", "09:01:05", "Tú"),
            ("09:04:00", "09:04:03", None),
            ("09:04:30", "09:04:33", "Persona 1"),
        ])
        self.assertFalse(result["complete"])
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["unknown"], [{"start": "10:00:00", "end": "10:30:00"}])
        self.assertIn("never exported", result["coverage"]["detail"])

    def test_a_window_that_starts_after_the_export_is_unknown_as_a_whole(self):
        result = Export(self.root, BASE + timedelta(hours=4)).transcript_since(30, False)
        self.assertEqual(result["phrases"], [])
        self.assertEqual(result["coverage"]["unknown"], [{"start": "12:30:00", "end": "13:00:00"}])
        self.assertFalse(result["complete"])

    def test_a_window_inside_the_export_has_no_unknown_stretch_but_is_still_not_complete(self):
        result = Export(self.root, BASE + timedelta(minutes=30)).transcript_since(30, False)
        self.assertEqual(result["coverage"]["unknown"], [])
        self.assertFalse(result["complete"])

    def test_a_phrase_counts_when_it_overlaps_the_window_and_not_when_it_ended_before(self):
        def times(now: datetime, minutes: int) -> list[str]:
            return [p["time"] for p in Export(self.root, now).transcript_since(minutes, False)["phrases"]]

        self.assertEqual(times(BASE + timedelta(minutes=5), 4), ["09:01:00", "09:04:00", "09:04:30"])
        self.assertEqual(times(BASE + timedelta(minutes=2, seconds=3), 1), ["09:01:00"])
        self.assertEqual(times(BASE + timedelta(minutes=1, seconds=15), 1), ["09:01:00"])

    def test_what_sounded_on_screen_stays_out_unless_asked(self):
        shown = self.export.transcript_since(90, True)["phrases"]
        self.assertEqual(len(shown), 5)
        self.assertEqual([p["on_screen"] for p in shown], [False, False, True, False, False])
        self.assertEqual(len(self.export.transcript_since(90, False)["phrases"]), 4)


class RecapTests(ExportFixture):
    def test_the_recap_and_the_conversations_have_the_live_shape(self):
        result = self.export.day_recap(TODAY)
        self.assertEqual(result["date"], "2026-10-09")
        self.assertEqual(result["recap"], {
            "headline": "Presupuesto del almacén cerrado",
            "summary": "Por la mañana se cerró el presupuesto.",
            "dayparts": [{"part": "mañana", "text": "Se cerró el presupuesto."}],
            "key_moments": [{"time": "09:01", "text": "Cifra final"}],
            "follow_ups": [{"text": "Enviar el presupuesto", "when": "mañana", "kind": "task"}],
            "people": ["Persona 1"],
            "places": ["Casa"],
            "final": False,
            "status": "generated",
            "engine": "AFM 3 Core Advanced · v1",
            "generated_at": "2026-10-09T09:55:00+02:00",
        })
        self.assertEqual(result["conversations"], [{
            "start": "09:00",
            "end": "09:04",
            "title": "Presupuesto del almacén",
            "gist": "Se habló del presupuesto y de la entrega.",
            "points": ["4.200 euros"],
            "people": ["Persona 1"],
            "follow_ups": [{"text": "Enviar el presupuesto", "when": None, "kind": "task"}],
            "status": "generated",
        }])
        self.assertEqual(result["places"], [{"name": "Casa", "start": "08:30", "end": "09:30"}])

    def test_a_day_without_a_recap_has_none_and_a_day_not_exported_is_empty(self):
        folder = self.root / "2026-10-09"
        plain = [line for line in (folder / "2026-10-09.jsonl").read_text(encoding="utf-8").splitlines() if json.loads(line)["type"] not in ("recap", "conversation")]
        (folder / "2026-10-09.jsonl").write_text("\n".join(plain) + "\n", encoding="utf-8")
        result = self.export.day_recap(TODAY)
        self.assertIsNone(result["recap"])
        self.assertEqual(result["conversations"], [])
        self.assertEqual(self.export.day_recap(date(2026, 9, 1)), {"date": "2026-09-01", "recap": None, "conversations": [], "places": []})

    def test_the_final_flag_is_kept(self):
        self.assertTrue(self.export.day_recap(YESTERDAY)["recap"]["final"])


class MarkingTests(ExportFixture):
    def test_every_answer_says_it_is_a_stale_export_and_why(self):
        for method, params in (("list_days", {}), ("read_day", {"day": "hoy"}), ("search", {"query": "proveedor"}), ("moments", {}), ("recent", {"hours": 2}), ("transcript_since", {"minutes": 30}), ("day_recap", {"day": "ayer"})):
            with self.subTest(method=method):
                result = self.answer(method, params)
                self.assertEqual(result["source"], "export (stale)")
                self.assertIs(result["stale"], True)
                self.assertEqual(result["iphone_unreachable"], REASON)
                self.assertNotIn("phone", result)
                self.assertNotIn("path", result)
                self.assertEqual(result["note"], link_export.NOTE)
                for claim in ("did not answer", "not live", "after as_of", "meaning search", "audio", "Health"):
                    self.assertIn(claim, result["note"])
                self.assertEqual(list(result)[:5], ["as_of", "source", "stale", "iphone_unreachable", "note"])
                self.assertNotIn("warnings", result)

    def test_the_answer_keeps_the_shape_of_the_tool(self):
        self.assertEqual(set(self.answer("list_days")) - {"as_of", "source", "stale", "iphone_unreachable", "note"}, {"days"})
        self.assertEqual(set(self.answer("read_day", {"day": "hoy"})) - {"as_of", "source", "stale", "iphone_unreachable", "note"}, {"date", "total_chars", "offset", "next_offset", "markdown"})
        self.assertEqual(set(self.answer("transcript_since", {"minutes": 30})) - {"as_of", "source", "stale", "iphone_unreachable", "note"}, {"window", "phrases", "coverage", "complete"})
        json.dumps(self.answer("day_recap", {"day": "hoy"}))

    def test_as_of_is_the_newest_time_the_export_was_written_for_tools_over_many_days(self):
        for method, params in (("list_days", {}), ("search", {"query": "proveedor"}), ("moments", {}), ("recent", {"hours": 2}), ("transcript_since", {"minutes": 30})):
            with self.subTest(method=method):
                self.assertEqual(self.answer(method, params)["as_of"], WRITTEN_TODAY)

    def test_as_of_is_the_own_time_of_the_day_for_read_day_and_day_recap(self):
        self.assertEqual(self.answer("read_day", {"day": "2026-10-08"})["as_of"], WRITTEN_YESTERDAY)
        self.assertEqual(self.answer("day_recap", {"day": "ayer"})["as_of"], WRITTEN_YESTERDAY)
        self.assertEqual(self.answer("read_day", {"day": "hoy"})["as_of"], WRITTEN_TODAY)
        self.assertEqual(self.answer("day_recap", {"day": "today"})["as_of"], WRITTEN_TODAY)

    def test_a_day_the_manifest_does_not_list_falls_back_to_the_newest_time(self):
        self.assertEqual(self.answer("read_day", {"day": "2026-10-01"})["as_of"], WRITTEN_TODAY)

    def test_as_of_carries_an_offset_even_when_the_manifest_says_zulu(self):
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        manifest["days"]["2026-10-09"] = "2026-10-09T08:00:00Z"
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.answer("list_days")["as_of"], "2026-10-09T08:00:00+00:00")

    def test_empty_results_are_results_not_errors(self):
        empty = self.answer("search", {"query": "inexistente"})
        self.assertEqual(empty["hits"], [])
        self.assertEqual(empty["as_of"], WRITTEN_TODAY)
        self.assertEqual(self.answer("read_day", {"day": "2026-01-01"})["markdown"], "")
        self.assertIsNone(self.answer("day_recap", {"day": "2026-01-01"})["recap"])

    def test_relative_days_follow_the_clock_given(self):
        later = datetime(2026, 10, 10, 8, 0, tzinfo=PLUS_TWO)
        self.assertEqual(self.answer("read_day", {"day": "ayer"}, now=later)["date"], "2026-10-09")
        self.assertEqual(self.answer("search", {"query": "proveedor", "since": "ayer"}, now=later)["hits"][0]["date"], "2026-10-09")
        self.assertEqual(self.answer("search", {"query": "proveedor", "since": "hoy"}, now=later)["hits"], [])

    def test_a_date_that_means_nothing_is_an_error_that_explains_the_formats(self):
        for params in ({"day": "el martes"}, {"day": ""}, {}):
            with self.subTest(params=params), self.assertRaisesRegex(ValueError, "hoy, ayer or YYYY-MM-DD"):
                self.answer("read_day", params)
        with self.assertRaisesRegex(ValueError, "Invalid date «mañana»"):
            self.answer("search", {"query": "x", "since": "mañana"})

    def test_a_method_the_export_cannot_answer_is_an_error(self):
        for method in ("status", "ping", "fail"):
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, "cannot answer"):
                self.answer(method)
        self.assertEqual(link_export.EXPORT_METHODS, {"list_days", "read_day", "search", "moments", "recent", "transcript_since", "day_recap"})


class ReadingTests(ExportFixture):
    def test_a_single_day_request_reads_only_that_day(self):
        for number in range(1, 8):
            day = f"2026-09-{number:02d}"
            (self.root / day).mkdir()
            (self.root / day / f"{day}.jsonl").mkdir()
            (self.root / day / f"{day}.md").mkdir()
        cases = (
            (lambda e: e.read_day(YESTERDAY, 0), ["2026-10-08/2026-10-08.md"]),
            (lambda e: e.day_recap(YESTERDAY), ["2026-10-08/2026-10-08.jsonl"]),
            (lambda e: e.search("proveedor", YESTERDAY, YESTERDAY, 20, False), ["2026-10-08/2026-10-08.jsonl"]),
            (lambda e: e.moments(TODAY, TODAY), ["2026-10-09/2026-10-09.jsonl"]),
            (lambda e: e.list_days(1), ["2026-10-09/2026-10-09.jsonl"]),
            (lambda e: e.recent(2, False), ["2026-10-08/2026-10-08.jsonl", "2026-10-09/2026-10-09.jsonl"]),
        )
        for call, expected in cases:
            with self.subTest(expected=expected):
                reads: list[str] = []
                recording = recording_export(reads)(self.root, NOW)
                call(recording)
                self.assertEqual(reads, ["Evoca/manifest.json"] + expected)
                self.assertEqual(recording.problems, [])

    def test_a_phrase_holding_a_unicode_line_separator_is_not_cut_in_two(self):
        odd = {"type": "utterance", "start": "2026-10-09T09:10:00+02:00", "end": "2026-10-09T09:10:04+02:00", "text": "Primera parte segunda parte\u0085tercera parte"}
        with open(self.root / "2026-10-09" / "2026-10-09.jsonl", "a", encoding="utf-8") as file:
            file.write(json.dumps(odd, ensure_ascii=False) + "\n")
        hit = self.export.search("segunda", None, None, 20, False)["hits"][0]
        self.assertEqual(hit["text"], odd["text"])
        self.assertEqual(self.export.problems, [])

    def test_nothing_is_ever_written_to_the_export(self):
        before = sorted((str(path.relative_to(self.root)), path.stat().st_mtime_ns, path.stat().st_size) for path in self.root.rglob("*"))
        for method, params in (("list_days", {}), ("read_day", {"day": "hoy"}), ("search", {"query": "proveedor"}), ("moments", {}), ("recent", {"hours": 30}), ("transcript_since", {"minutes": 60}), ("day_recap", {"day": "hoy"})):
            self.answer(method, params)
        after = sorted((str(path.relative_to(self.root)), path.stat().st_mtime_ns, path.stat().st_size) for path in self.root.rglob("*"))
        self.assertEqual(before, after)


class DamagedExportTests(ExportFixture):
    def damage(self):
        (self.root / "2026-10-07").mkdir()
        good = json.dumps({"type": "utterance", "start": "2026-10-07T09:00:00+02:00", "end": "2026-10-07T09:00:03+02:00", "text": "Una frase que sí se lee"}, ensure_ascii=False)
        lines = ["{\"type\": \"utterance\", \"start\": \"2026", "[1, 2]", "null", good, json.dumps({"type": "utterance", "start": "no es una hora", "end": "x", "text": "mala"}), json.dumps({"type": "utterance", "start": "2026-10-07T09:00:05+02:00", "end": "2026-10-07T09:00:06+02:00"}), json.dumps({"type": "unknown-future-kind", "x": 1}), ""]
        (self.root / "2026-10-07" / "2026-10-07.jsonl").write_text("\n".join(lines), encoding="utf-8")
        (self.root / "2026-10-06").mkdir()
        (self.root / "2026-10-06" / "2026-10-06.md").write_text("# Solo Markdown\n", encoding="utf-8")
        (self.root / "2026-10-05").mkdir()
        (self.root / "2026-10-05" / "2026-10-05.jsonl").write_bytes(b"\xff\xfe\x00 not utf-8")
        (self.root / "2026-10-04" / "2026-10-04.jsonl").mkdir(parents=True)
        (self.root / "2026-10-03").write_text("a file named like a day", encoding="utf-8")
        (self.root / "notes").mkdir()

    def test_damaged_and_missing_day_files_cost_only_what_is_damaged(self):
        self.damage()
        with self.assertLogs("evoca-link", level="WARNING") as logged:
            result = self.answer("list_days", {"limit": 20})
        days = {row["date"]: row for row in result["days"]}
        self.assertEqual(list(days), ["2026-10-09", "2026-10-08", "2026-10-07"])
        self.assertEqual(days["2026-10-07"]["phrases"], 1)
        self.assertIn("2026-10-07.jsonl: 5 unreadable line(s) skipped", result["warnings"])
        self.assertTrue(any("2026-10-05.jsonl could not be read (UnicodeDecodeError)" in warning for warning in result["warnings"]))
        self.assertTrue(any("2026-10-04.jsonl could not be read" in warning for warning in result["warnings"]))
        self.assertEqual(len(logged.records), len(result["warnings"]))
        self.assertNotIn(self.temp.name, "".join(result["warnings"]))

    def test_the_good_lines_of_a_damaged_day_are_still_searched(self):
        self.damage()
        with self.assertLogs("evoca-link", level="WARNING"):
            result = self.answer("search", {"query": "frase"})
        self.assertEqual([hit["date"] for hit in result["hits"]], ["2026-10-07"])

    def test_a_day_with_only_markdown_still_reads_and_has_no_recap(self):
        self.damage()
        self.assertEqual(self.answer("read_day", {"day": "2026-10-06"})["markdown"], "# Solo Markdown\n")
        self.assertIsNone(self.answer("day_recap", {"day": "2026-10-06"})["recap"])

    def test_an_unreadable_day_gives_an_empty_answer_and_a_warning(self):
        self.damage()
        with self.assertLogs("evoca-link", level="WARNING"):
            result = self.answer("day_recap", {"day": "2026-10-05"})
        self.assertEqual((result["recap"], result["conversations"], result["places"]), (None, [], []))
        self.assertEqual(len(result["warnings"]), 1)

    def test_a_damaged_manifest_falls_back_to_its_modification_time(self):
        (self.root / "manifest.json").write_text("{\"format\": \"evoca.export/1\", \"days\": [", encoding="utf-8")
        with self.assertLogs("evoca-link", level="WARNING"):
            result = self.answer("list_days")
        self.assertIn("manifest.json could not be understood", result["warnings"][0])
        self.assertIsNotNone(datetime.fromisoformat(result["as_of"]).utcoffset())
        self.assertEqual(len(result["days"]), 2)

    def test_a_manifest_with_the_wrong_kind_of_times_is_not_trusted(self):
        (self.root / "manifest.json").write_text(json.dumps({"format": "evoca.export/1", "days": {"2026-10-09": 5}}), encoding="utf-8")
        with self.assertLogs("evoca-link", level="WARNING"):
            result = self.answer("list_days")
        self.assertIsNotNone(result["as_of"])
        self.assertEqual(len(result["warnings"]), 1)

    def test_a_manifest_that_vanished_leaves_as_of_unknown_instead_of_failing(self):
        (self.root / "manifest.json").unlink()
        result = self.answer("list_days")
        self.assertIsNone(result["as_of"])
        self.assertEqual(len(result["days"]), 2)
        self.assertNotIn("warnings", result)

    def test_an_export_folder_that_cannot_be_listed_is_an_empty_answer_with_a_warning(self):
        missing = Path(self.temp.name) / "gone"
        with self.assertLogs("evoca-link", level="WARNING"):
            result = answer_from_export(missing, "list_days", {}, REASON, NOW)
        self.assertEqual(result["days"], [])
        self.assertTrue(result["warnings"])


class ParsingTests(unittest.TestCase):
    def test_relative_and_iso_days(self):
        today = date(2026, 10, 10)
        self.assertEqual(parse_day("hoy", today), today)
        self.assertEqual(parse_day("Today", today), today)
        self.assertEqual(parse_day("Ayer", today), date(2026, 10, 9))
        self.assertEqual(parse_day(" yesterday ", today), date(2026, 10, 9))
        self.assertEqual(parse_day("2026-10-01", today), date(2026, 10, 1))
        self.assertIsNone(parse_day(None, today))
        self.assertIsNone(parse_day("  ", today))

    def test_times_with_zulu_or_no_offset_become_aware(self):
        self.assertEqual(parse_time("2026-10-09T08:00:00Z").utcoffset(), timedelta(0))
        self.assertEqual(parse_time("2026-10-09T08:00:00+02:00").utcoffset(), timedelta(hours=2))
        self.assertIsNotNone(parse_time("2026-10-09T08:00:00").utcoffset())

    def test_the_module_needs_nothing_but_the_standard_library(self):
        tree = ast.parse(Path(link_export.__file__).read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertLessEqual(imported, set(sys.stdlib_module_names))


if __name__ == "__main__":
    unittest.main()
