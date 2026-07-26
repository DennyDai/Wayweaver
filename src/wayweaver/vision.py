import asyncio
import csv
import io
import re
import shutil
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from .adapters.base import Adapter
from .errors import ActionError, CapabilityError
from .types import Frame


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    token: str
    confidence: float
    left: int
    top: int
    width: int
    height: int
    line: tuple[str, str, str, str]


def tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)


def parse_tsv(tsv: str) -> list[Word]:
    words = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if not text or row.get("level") != "5":
            continue
        try:
            base = {
                "confidence": float(row["conf"]),
                "left": int(row["left"]),
                "top": int(row["top"]),
                "width": int(row["width"]),
                "height": int(row["height"]),
                "line": (
                    row["page_num"],
                    row["block_num"],
                    row["par_num"],
                    row["line_num"],
                ),
            }
            words.extend(Word(text, token, **base) for token in tokens(text))
        except (KeyError, TypeError, ValueError):
            continue
    return words


async def recognize(frame: Frame, shell: Adapter | None = None) -> list[Word]:
    arguments = ["stdin", "stdout", "tsv"]
    if command := shutil.which("tesseract"):
        process = await asyncio.create_subprocess_exec(
            command,
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(frame.png)
        if process.returncode:
            raise ActionError(stderr.decode(errors="replace").strip())
        return parse_tsv(stdout.decode(errors="replace"))
    if shell:
        code, stdout, stderr = await shell.shell(
            "tesseract stdin stdout tsv", frame.png
        )
        if code:
            raise ActionError(stderr.decode(errors="replace").strip())
        return parse_tsv(stdout.decode(errors="replace"))
    raise CapabilityError(
        "OCR requires local tesseract or an SSH target with tesseract"
    )


def find_text(words: list[Word], locator: str | dict[str, Any]) -> dict[str, Any]:
    options = {"text": locator} if isinstance(locator, str) else locator
    text = options.get("text")
    if not isinstance(text, str):
        raise ActionError("text locator requires text")
    wanted = tokens(text)
    if not wanted:
        raise ActionError("text locator is empty")
    contains = bool(options.get("contains", False))
    fuzzy = bool(options.get("fuzzy", False))
    similarity = float(options.get("similarity", 0.8))
    if not 0 < similarity <= 1:
        raise ActionError("similarity must be greater than 0 and at most 1")
    region = options.get("region")
    if region is not None:
        if not isinstance(region, list) or len(region) != 4:
            raise ActionError("region must be [left, top, right, bottom]")
        left_bound, top_bound, right_bound, bottom_bound = map(int, region)
        if right_bound <= left_bound or bottom_bound <= top_bound:
            raise ActionError("region right/bottom must exceed left/top")
    matches = []
    size = len(wanted)
    # How a recognizer splits a phrase into words is not stable: the same
    # button read as "Don't" in one crop and "Dont" in another, which is three
    # tokens against two and could never match token-for-token. Comparing the
    # tokens joined together is indifferent to where the splits fall, so an
    # exact match spans however many words the recognizer happened to produce.
    wanted_joined = "".join(wanted)
    groups: list[tuple[list[Word], bool]] = []
    if not contains:
        for index in range(len(words)):
            joined = ""
            for end in range(index, len(words)):
                word = words[end]
                if end > index and word.line != words[index].line:
                    break
                joined += word.token
                if not wanted_joined.startswith(joined):
                    break
                if joined == wanted_joined:
                    groups.append((words[index : end + 1], True))
                    break
    if contains or fuzzy:
        for index in range(len(words) - size + 1):
            group = words[index : index + size]
            if size > 1 and any(word.line != group[0].line for word in group[1:]):
                continue
            seen = [word.token for word in group]
            if contains and all(
                target in value for target, value in zip(wanted, seen)
            ):
                groups.append((group, True))
            elif fuzzy and all(
                SequenceMatcher(None, target, value).ratio() >= similarity
                for target, value in zip(wanted, seen)
            ):
                groups.append((group, False))
    seen_spans: set[tuple[int, int, int, int]] = set()
    for group, exact in groups:
        span = (group[0].left, group[0].top, group[-1].left, group[-1].top)
        if span in seen_spans:
            continue
        seen_spans.add(span)
        seen = [word.token for word in group]
        left = min(word.left for word in group)
        top = min(word.top for word in group)
        right = max(word.left + word.width for word in group)
        bottom = max(word.top + word.height for word in group)
        x, y = round((left + right) / 2), round((top + bottom) / 2)
        matches.append(
            {
                "text": " ".join(seen),
                "x": x,
                "y": y,
                "box": {
                    "left": left,
                    "top": top,
                    "width": right - left,
                    "height": bottom - top,
                },
                "confidence": round(
                    sum(word.confidence for word in group) / len(group), 1
                ),
                "match": "exact" if exact else "fuzzy",
            }
        )
    all_matches = len(matches)
    if region is not None:
        matches = [
            match
            for match in matches
            if left_bound <= match["x"] <= right_bound
            and top_bound <= match["y"] <= bottom_bound
        ]
    # Picking the first of several matches silently clicks whichever the
    # recognizer happened to order first -- a URL bar rather than the button
    # bearing the same word. `strict` turns that into a refusal.
    if options.get("strict") and "nth" not in options and len(matches) > 1:
        raise ActionError(
            f"ambiguous text locator {text!r}: {len(matches)} matches; "
            "narrow it with region, window, or nth",
            details={"matches": len(matches), "total_matches": all_matches},
        )
    nth = int(options.get("nth", 0))
    nth = nth if nth >= 0 else len(matches) + nth
    if not 0 <= nth < len(matches):
        raise ActionError(
            f"text not found: {text!r}; {len(matches)} regional matches, {all_matches} total"
        )
    result = dict(matches[nth])
    result.update({"matches": len(matches), "total_matches": all_matches})
    if region is not None:
        result["region"] = list(map(int, region))
    return result


def ocr_items(words: list[Word]) -> list[dict[str, Any]]:
    items = []
    seen = set()
    for word in words:
        identity = (
            word.line,
            word.text,
            word.left,
            word.top,
            word.width,
            word.height,
        )
        if identity in seen:
            continue
        seen.add(identity)
        items.append(
            {
                "text": word.text,
                "confidence": round(word.confidence, 1),
                "point": {
                    "x": round(word.left + word.width / 2),
                    "y": round(word.top + word.height / 2),
                },
                "box": {
                    "left": word.left,
                    "top": word.top,
                    "width": word.width,
                    "height": word.height,
                },
            }
        )
    return items


def text_output(words: list[Word]) -> str:
    """Join recognized words into lines of text.

    Duplicates are identified by position as well as by text. Comparing only
    against the previous word collapsed genuine repetition -- a row reading
    "Total 1 1 1" came back as "Total 1" -- which silently rewrites the text an
    agent is reading. A word the recognizer really did emit twice occupies the
    same box, so the box is what distinguishes them.
    """
    lines: dict[tuple[str, str, str, str], list[str]] = {}
    seen: set[tuple[Any, ...]] = set()
    for word in words:
        identity = (word.line, word.text, word.left, word.top, word.width, word.height)
        if identity in seen:
            continue
        seen.add(identity)
        lines.setdefault(word.line, []).append(word.text)
    return "\n".join(" ".join(line) for line in lines.values())
