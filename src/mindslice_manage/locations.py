"""Locations (a file, a regex with one capture group, an expected match count) and version strings."""

import re
from dataclasses import dataclass, field
from pathlib import Path

VERSION_RE = re.compile(r"\d+(?:\.\d+)*(?:-\d+)?")
VALUE_RES = {
    "version": VERSION_RE,
    "commit": re.compile(r"[0-9a-f]{40}"),
    "wheel_url": re.compile(r"https://\S+\.whl"),
    "wheel_sha256": re.compile(r"[0-9a-f]{64}"),
}
FORMATS = ("version", "tuple")


@dataclass
class Location:
    file: str
    regex: str
    count: int = 1
    format: str = "version"
    value: str = "version"
    update: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.file}: {self.regex}"


class LocationError(Exception):
    pass


# versions: "0.2.3", "0.2.3-1" (a build number after the dash), or the tuple form "0, 2, 3"

def parse_version(text: str, fmt: str = "version") -> str:
    """The dotted form of a captured version; ValueError if it is not one."""
    text = text.strip()
    if fmt == "tuple":
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if not parts or not all(p.isdigit() for p in parts):
            raise ValueError(f"not a version tuple: {text!r}")
        return ".".join(str(int(p)) for p in parts)
    if not VERSION_RE.fullmatch(text):
        raise ValueError(f"not a version: {text!r}")
    return text


def render_version(version: str, fmt: str = "version") -> str:
    return ", ".join(version.split("-")[0].split(".")) if fmt == "tuple" else version


def version_key(version: str) -> tuple:
    """Sort key: release parts, then the build number (no build sorts first)."""
    base, _, build = version.partition("-")
    return tuple(int(p) for p in base.split(".")), int(build) if build else -1


def release_part(version: str) -> str:
    return version.partition("-")[0]


def build_part(version: str) -> int | None:
    _, _, build = version.partition("-")
    return int(build) if build else None


def bumped(version: str, part: str) -> str:
    """version with one part raised: patch, minor, major (three-part releases) or build (the -n suffix)."""
    base, _, build = version.partition("-")
    if part == "build":
        return f"{base}-{int(build) + 1 if build else 1}"
    nums = [int(p) for p in base.split(".")]
    while len(nums) < 3:
        nums.append(0)
    i = {"major": 0, "minor": 1, "patch": 2}[part]
    nums[i] += 1
    nums[i + 1:3] = [0] * (2 - i)
    return ".".join(map(str, nums))


# reading and rewriting

def read_text(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as f:  # newline="": keep CRLF as it is
        return f.read()


def write_text(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def matches(repo: Path, loc: Location) -> list[re.Match]:
    """The location's matches; LocationError if the file is missing or the count is not the expected one."""
    path = repo / loc.file
    if not path.is_file():
        raise LocationError(f"{loc.file}: file not found")
    found = list(re.finditer(loc.regex, read_text(path)))
    if len(found) != loc.count:
        raise LocationError(f"{loc.file}: regex {loc.regex!r} matches {len(found)} times, expected {loc.count}")
    return found


def read_value(repo: Path, loc: Location) -> str:
    """The value at a location (normalized for versions); LocationError if the matches disagree or do not parse."""
    raws = {m.group(1) for m in matches(repo, loc)}
    if len(raws) > 1:
        raise LocationError(f"{loc.file}: the {loc.count} matches differ: {', '.join(sorted(raws))}")
    raw = raws.pop()
    if loc.value == "version":
        try:
            return parse_version(raw, loc.format)
        except ValueError as e:
            raise LocationError(f"{loc.file}: {e}") from None
    if not VALUE_RES[loc.value].fullmatch(raw):
        raise LocationError(f"{loc.file}: {raw!r} is not a {loc.value}")
    return raw


def rewrite(repo: Path, loc: Location, value: str) -> bool:
    """Replace the captured group of every match with value (rendered in the location's format); True if changed."""
    new = render_version(value, loc.format) if loc.value == "version" else value
    path = repo / loc.file
    text = read_text(path)
    out, pos = [], 0
    for m in matches(repo, loc):
        out += [text[pos:m.start(1)], new]
        pos = m.end(1)
    out.append(text[pos:])
    result = "".join(out)
    if result == text:
        return False
    write_text(path, result)
    return True


def spans(repo: Path, loc: Location) -> list[tuple[int, int]]:
    try:
        return [m.span(1) for m in re.finditer(loc.regex, read_text(repo / loc.file))]
    except OSError:
        return []
