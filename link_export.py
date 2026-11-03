import json
import logging
import os
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import islice
from pathlib import Path

FORMAT = "evoca.export/1"
EXPORT_DIR_VARIABLE = "EVOCA_EXPORT_DIR"
SEARCH_DEPTH = 4
CONTEXT_SECONDS = 45
DAY_CHUNK_CHARS = 40_000
MAX_RECENT_PHRASES = 600
MAX_MOMENTS = 200
# «Lugar 2» or «Un lugar» tells a reader nothing; the day recap leaves them out too.
UNNAMED_PLACE_SOURCES = {"frequent", "unknown"}
EXPORT_METHODS = frozenset({"list_days", "read_day", "search", "moments", "recent", "transcript_since", "day_recap"})
DAY_METHODS = frozenset({"read_day", "day_recap"})
MALFORMED = (KeyError, ValueError, TypeError, AttributeError)
SOURCE = "export (stale)"
NOTE = "The iPhone did not answer, so this is the latest export of Evoca on this computer, not live data: it lacks anything recorded after as_of and has no meaning search, audio or Health data."
COVERAGE_DETAIL = "Only phrases already transcribed and exported are listed: the minutes after the export was written, what the watch still holds, audio waiting for transcription and any minute that was never exported are unknown, so a gap here does not mean silence."
HOW_TO_POINT = f"turn on «Export Automatically» (Exportar automáticamente in Spanish) in Evoca › Settings › Agents and Export, or set {EXPORT_DIR_VARIABLE} to the exported Evoca folder (the one that holds manifest.json)"

log = logging.getLogger("evoca-link")


@dataclass(frozen=True)
class Utterance:
    start: datetime
    end: datetime
    text: str
    speaker: str | None
    on_screen: bool


@dataclass(frozen=True)
class Moment:
    at: datetime
    title: str | None
    note: str | None


@dataclass(frozen=True)
class Place:
    start: datetime
    end: datetime
    name: str


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def parse_time(value: str) -> datetime:
    # fromisoformat reads a trailing Z only from Python 3.11.
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    return parsed if parsed.tzinfo else parsed.astimezone()


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def clock(value: datetime) -> str:
    return value.strftime("%H:%M:%S")


def minute(value: datetime) -> str:
    return value.strftime("%H:%M")


def required_text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected text")
    return value


def optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def texts(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def follow_up(item: dict) -> dict:
    return {"text": required_text(item["text"]), "when": optional_text(item.get("when")), "kind": required_text(item["kind"])}


def conversation_view(event: dict) -> dict:
    return {
        "start": minute(parse_time(event["start"])),
        "end": minute(parse_time(event["end"])),
        "title": required_text(event["title"]),
        "gist": required_text(event["gist"]),
        "points": texts(event.get("points")),
        "people": texts(event.get("people")),
        "follow_ups": [follow_up(item) for item in event.get("followUps") or []],
        "status": optional_text(event.get("status")) or "generated",
    }


def recap_view(event: dict) -> dict:
    return {
        "headline": required_text(event["headline"]),
        "summary": required_text(event["summary"]),
        "dayparts": [{"part": required_text(p["part"]), "text": required_text(p["text"])} for p in event.get("dayparts") or []],
        "key_moments": [{"time": minute(parse_time(m["at"])), "text": required_text(m["text"])} for m in event.get("keyMoments") or []],
        "follow_ups": [follow_up(item) for item in event.get("followUps") or []],
        "people": texts(event.get("people")),
        "places": texts(event.get("places")),
        "final": event.get("final") is True,
        "status": optional_text(event.get("status")),
        "engine": optional_text(event.get("engine")),
        "generated_at": required_text(event["generatedAt"]),
    }


@dataclass
class Day:
    day: date
    utterances: list[Utterance] = field(default_factory=list)
    moments: list[Moment] = field(default_factory=list)
    places: list[Place] = field(default_factory=list)
    conversations: list[dict] = field(default_factory=list)
    recap: dict | None = None
    recorded_seconds: float = 0.0

    def add(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "segment":
            self.recorded_seconds += max(0.0, (parse_time(event["end"]) - parse_time(event["start"])).total_seconds())
        elif kind == "utterance":
            self.utterances.append(Utterance(parse_time(event["start"]), parse_time(event["end"]), required_text(event["text"]), optional_text(event.get("speaker")), event.get("onScreen") is True))
        elif kind == "moment":
            self.moments.append(Moment(parse_time(event["at"]), optional_text(event.get("title")), optional_text(event.get("note"))))
        elif kind == "place":
            if event.get("source") not in UNNAMED_PLACE_SOURCES:
                self.places.append(Place(parse_time(event["start"]), parse_time(event["end"]), required_text(event["name"])))
        elif kind == "conversation":
            self.conversations.append(conversation_view(event))
        elif kind == "recap":
            self.recap = recap_view(event)

    def finish(self) -> None:
        self.utterances.sort(key=lambda u: u.start)
        self.moments.sort(key=lambda m: m.at)
        self.conversations.sort(key=lambda c: c["start"])

    def place_at(self, instant: datetime) -> str | None:
        return next((p.name for p in self.places if p.start <= instant <= p.end), None)

    def phrase(self, utterance: Utterance) -> dict:
        return {"date": self.day.isoformat(), "time": clock(utterance.start), "speaker": utterance.speaker, "place": self.place_at(utterance.start), "text": utterance.text}

    def hit(self, index: int) -> dict:
        utterance = self.utterances[index]
        before = self.utterances[index - 1].text if index > 0 else None
        after = self.utterances[index + 1].text if index + 1 < len(self.utterances) else None
        return {**self.phrase(utterance), "before": before, "after": after, "match": "words", "on_screen": utterance.on_screen}

    def moment_rows(self) -> list[dict]:
        rows = []
        for moment in self.moments:
            low = moment.at - timedelta(seconds=CONTEXT_SECONDS)
            high = moment.at + timedelta(seconds=CONTEXT_SECONDS)
            around = [u.text for u in self.utterances if u.end >= low and u.start <= high and not u.on_screen]
            rows.append({"date": self.day.isoformat(), "time": clock(moment.at), "title": moment.title, "note": moment.note, "place": self.place_at(moment.at), "said_around": " ".join(around)})
        return rows

    def row(self) -> dict:
        return {
            "date": self.day.isoformat(),
            "recorded_minutes": round(self.recorded_seconds / 60),
            "phrases": sum(1 for u in self.utterances if not u.on_screen),
            "moments": len(self.moments),
            "headline": self.recap["headline"] if self.recap else None,
            "places": list(dict.fromkeys(p.name for p in self.places)),
        }


def provider_roots() -> list[Path]:
    home = Path.home()
    roots = [home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"]
    try:
        roots += sorted(p for p in (home / "Library" / "CloudStorage").iterdir() if p.is_dir())
    except OSError:
        pass
    return roots


def has_manifest(folder: Path) -> bool:
    try:
        return (folder / "manifest.json").is_file()
    except OSError:
        return False


def manifests_under(base: Path, depth: int) -> Iterator[Path]:
    if depth < 0:
        return
    manifest = base / "manifest.json"
    try:
        if json.loads(manifest.read_text(encoding="utf-8")).get("format") == FORMAT:
            yield manifest
            return
    except (OSError, ValueError, AttributeError):
        pass
    try:
        children = sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return
    for child in children:
        yield from manifests_under(child, depth - 1)


def find_export_root(explicit: str | None = None, providers: list[Path] | None = None) -> Path:
    for configured in (explicit, os.environ.get(EXPORT_DIR_VARIABLE)):
        if configured:
            root = Path(configured).expanduser()
            for folder in (root, root / "Evoca"):
                if has_manifest(folder):
                    return folder
            raise FileNotFoundError(f"no manifest.json in {root}")
    for provider in provider_roots() if providers is None else providers:
        for manifest in manifests_under(provider, SEARCH_DEPTH):
            return manifest.parent
    raise FileNotFoundError("none in iCloud Drive or ~/Library/CloudStorage")


def parse_day(value: str | None, today: date) -> date | None:
    if value is None or not value.strip():
        return None
    word = fold(value.strip())
    if word in ("hoy", "today"):
        return today
    if word in ("ayer", "yesterday"):
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"Invalid date «{value}»: use hoy, ayer or YYYY-MM-DD.") from None


def required_day(value: str | None, today: date) -> date:
    day = parse_day(value, today)
    if day is None:
        raise ValueError("Missing date: use hoy, ayer or YYYY-MM-DD.")
    return day


def chunk(text: str, offset: int, size: int) -> tuple[str, int | None]:
    start = max(0, min(offset, len(text)))
    end = min(len(text), start + size)
    if end < len(text):
        line_break = text.rfind("\n", start, end)
        if line_break > start:
            end = line_break + 1
    return text[start:end], (end if end < len(text) else None)


class Export:
    def __init__(self, root: Path, now: datetime | None = None):
        self.root = root
        self.now = now or datetime.now().astimezone()
        self.problems: list[str] = []
        self.times = self.manifest_times()

    def problem(self, message: str) -> None:
        self.problems.append(message)
        log.warning("export: %s", message)

    def read(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as error:
            self.problem(f"{path.name} could not be read ({type(error).__name__})")
            return None

    def manifest_times(self) -> dict[str, datetime]:
        text = self.read(self.root / "manifest.json")
        if text is None:
            return {}
        try:
            return {day: parse_time(stamp) for day, stamp in json.loads(text)["days"].items()}
        except MALFORMED:
            self.problem("manifest.json could not be understood, so its modification time stands for when the export was written")
            return {}

    def last_written(self, day: date | None = None) -> datetime | None:
        if day is not None and day.isoformat() in self.times:
            return self.times[day.isoformat()]
        if self.times:
            return max(self.times.values())
        try:
            return datetime.fromtimestamp((self.root / "manifest.json").stat().st_mtime).astimezone()
        except OSError:
            return None

    def folders(self, first: date | None, last: date | None, newest_first: bool) -> list[date]:
        try:
            with os.scandir(self.root) as entries:
                names = [entry.name for entry in entries if entry.is_dir()]
        except OSError as error:
            self.problem(f"the export folder could not be listed ({type(error).__name__})")
            return []
        days = []
        for name in names:
            try:
                day = date.fromisoformat(name)
            except ValueError:
                continue
            if (first is None or day >= first) and (last is None or day <= last):
                days.append(day)
        return sorted(days, reverse=newest_first)

    def load(self, day: date) -> Day | None:
        name = day.isoformat()
        text = self.read(self.root / name / f"{name}.jsonl")
        if text is None:
            return None
        loaded = Day(day)
        unreadable = 0
        # splitlines would also cut a phrase that holds U+2028 or U+0085.
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                loaded.add(json.loads(line))
            except MALFORMED:
                unreadable += 1
        if unreadable:
            self.problem(f"{name}.jsonl: {unreadable} unreadable line(s) skipped")
        loaded.finish()
        return loaded

    def days(self, first: date | None = None, last: date | None = None, newest_first: bool = True) -> Iterator[Day]:
        for day in self.folders(first, last, newest_first):
            loaded = self.load(day)
            if loaded is not None:
                yield loaded

    def around_now(self, since: datetime) -> Iterator[Day]:
        # A day folder is the iPhone's day, which can differ from this computer's around midnight or after a trip.
        return self.days(since.date() - timedelta(days=1), self.now.date() + timedelta(days=1), newest_first=False)

    def list_days(self, limit: int) -> dict:
        return {"days": [day.row() for day in islice(self.days(), limit)]}

    def read_day(self, day: date, offset: int) -> dict:
        name = day.isoformat()
        markdown = self.read(self.root / name / f"{name}.md") or ""
        piece, next_offset = chunk(markdown, offset, DAY_CHUNK_CHARS)
        return {"date": name, "total_chars": len(markdown), "offset": max(0, offset), "next_offset": next_offset, "markdown": piece}

    def search(self, query: str, first: date | None, last: date | None, limit: int, include_on_screen: bool) -> dict:
        wanted = query.strip()
        words = fold(wanted).split()
        terms = [word for word in words if len(word) > 2] or words
        found = []
        for day in self.days(first, last):
            for index, utterance in enumerate(day.utterances):
                if utterance.on_screen and not include_on_screen:
                    continue
                folded = fold(utterance.text)
                score = sum(1 for term in terms if term in folded)
                if score:
                    found.append((score, utterance.start, day.hit(index)))
        best = max((score for score, _, _ in found), default=0)
        required = len(terms) if best == len(terms) else 1
        ranked = sorted((item for item in found if item[0] >= required), key=lambda item: (-item[0], -item[1].timestamp()))
        return {"query": wanted, "hits": [hit for _, _, hit in ranked[:limit]]}

    def moments(self, first: date | None, last: date | None) -> dict:
        rows: list[dict] = []
        for day in self.days(first, last):
            rows = day.moment_rows() + rows
            if len(rows) > MAX_MOMENTS:
                break
        return {"moments": rows[-MAX_MOMENTS:], "truncated": len(rows) > MAX_MOMENTS}

    def recent(self, hours: int, include_on_screen: bool) -> dict:
        since = self.now - timedelta(hours=hours)
        found = [
            (utterance.start, day.phrase(utterance))
            for day in self.around_now(since)
            for utterance in day.utterances
            if since <= utterance.start <= self.now and (include_on_screen or not utterance.on_screen)
        ]
        phrases = [phrase for _, phrase in sorted(found, key=lambda item: item[0])]
        return {"hours": hours, "phrases": phrases[-MAX_RECENT_PHRASES:], "truncated": len(phrases) > MAX_RECENT_PHRASES}

    def transcript_since(self, minutes: int, include_on_screen: bool) -> dict:
        since = self.now - timedelta(minutes=minutes)
        found = [
            (utterance.start, {"date": day.day.isoformat(), "time": clock(utterance.start), "end": clock(utterance.end), "speaker": utterance.speaker, "place": day.place_at(utterance.start), "text": utterance.text, "on_screen": utterance.on_screen})
            for day in self.around_now(since)
            for utterance in day.utterances
            if utterance.end >= since and utterance.start <= self.now and (include_on_screen or not utterance.on_screen)
        ]
        written = self.last_written()
        known_until = max(since, written) if written else since
        unknown = [{"start": clock(known_until), "end": clock(self.now)}] if known_until < self.now else []
        return {
            "window": {"start": iso(since), "end": iso(self.now), "minutes": minutes},
            "phrases": [phrase for _, phrase in sorted(found, key=lambda item: item[0])],
            "coverage": {"complete": False, "unknown": unknown, "detail": COVERAGE_DETAIL},
            "complete": False,
        }

    def day_recap(self, day: date) -> dict:
        loaded = self.load(day)
        if loaded is None:
            return {"date": day.isoformat(), "recap": None, "conversations": [], "places": []}
        places = [{"name": p.name, "start": minute(p.start), "end": minute(p.end)} for p in loaded.places]
        return {"date": day.isoformat(), "recap": loaded.recap, "conversations": loaded.conversations, "places": places}


def answer_from_export(root: Path, method: str, params: dict, unreachable: str, now: datetime | None = None) -> dict:
    export = Export(root, now)
    today = export.now.date()
    day = required_day(params.get("day"), today) if method in DAY_METHODS else None
    if method == "list_days":
        result = export.list_days(params.get("limit", 14))
    elif method == "read_day":
        result = export.read_day(day, params.get("offset", 0))
    elif method == "search":
        result = export.search(params.get("query", ""), parse_day(params.get("since"), today), parse_day(params.get("until"), today), params.get("limit", 20), params.get("include_on_screen", False))
    elif method == "moments":
        result = export.moments(parse_day(params.get("since"), today), parse_day(params.get("until"), today))
    elif method == "recent":
        result = export.recent(params.get("hours", 3), params.get("include_on_screen", False))
    elif method == "transcript_since":
        result = export.transcript_since(params.get("minutes", 15), params.get("include_on_screen", False))
    elif method == "day_recap":
        result = export.day_recap(day)
    else:
        raise ValueError(f"The export cannot answer {method}.")
    written = export.last_written(day)
    answer = {"as_of": iso(written) if written else None, "source": SOURCE, "stale": True, "iphone_unreachable": unreachable, "note": NOTE, **result}
    if export.problems:
        answer["warnings"] = list(export.problems)
    return answer


def export_summary(root: Path) -> dict:
    written = Export(root).last_written()
    return {"found": True, "path": str(root), "last_written": iso(written) if written else None}
