"""The animation frame receipt servers hand to each other, and the bounded command that fills it.

A receipt pins ordered RGBA PNG frames and their source files by SHA-256 inside one
workspace. Needs Pillow: install ``cavi-mcp-core[media]``. ``run`` is POSIX only.
"""

import hashlib
import json
import math
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from PIL import Image

SCHEMA = "cavi.animation-media.v1"
MAX_OUTPUT_BYTES = 512 * 1024 * 1024


class MediaOutcomeUnknown(RuntimeError):
    """A native command started; inspect its new directory before another action."""


def digest(path):
    """SHA-256 of a file."""
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inside(workspace, path):
    """``path`` resolved inside ``workspace``; refuses escapes and the root itself."""
    if not isinstance(path, (str, Path)) or not str(path) or "\x00" in str(path):
        raise ValueError("Use a nonempty workspace path")
    root = Path(workspace).resolve(strict=True)
    result = (root / path).resolve()
    if not result.is_relative_to(root) or result == root:
        raise ValueError("Use a path inside the shared workspace")
    return result


def load(path):
    """JSON of a file of at most 4 MiB."""
    if Path(path).stat().st_size > 4 * 1024 * 1024:
        raise ValueError("JSON must be at most 4 MiB")
    return json.loads(Path(path).read_text())


def write(path, value):
    """Write ``value`` as JSON to a new file; refuses an existing one."""
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def name(value):
    """A receipt or frame name: a letter, then at most 63 ASCII letters, digits, _ or -."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
        raise ValueError(
            "Names must start with a letter and contain at most 64 ASCII letters, digits, _ or -")
    return value


def rate(value):
    """A rational frame rate of 1..120 FPS, reduced to lowest terms."""
    if (not isinstance(value, dict) or set(value) != {"numerator", "denominator"}
            or any(type(x) is not int or not 1 <= x <= 120000 for x in value.values())
            or not 1 <= value["numerator"] / value["denominator"] <= 120):
        raise ValueError("Declare rational FPS numerator/denominator in 1..120 FPS")
    divisor = math.gcd(value["numerator"], value["denominator"])
    return {key: item // divisor for key, item in value.items()}


def frames(workspace, value, *, pinned):
    """Check 1..1000 ordered frames; ``pinned`` frames must match their recorded PNG."""
    if not isinstance(value, list) or not 1 <= len(value) <= 1000:
        raise ValueError("Declare 1..1000 ordered frames")
    result, images, names, encoded, pixels = [], {}, set(), 0, 0
    for frame in value:
        required = ({"name", "path", "sha256", "width", "height", "rect"} if pinned
                    else {"name", "path"})
        if (not isinstance(frame, dict) or not required <= set(frame)
                or set(frame) - (required | {"rect"})):
            raise ValueError("Unexpected or missing frame fields")
        label = name(frame["name"])
        if label in names:
            raise ValueError("Frame names must be unique")
        names.add(label)
        path = inside(workspace, frame["path"])
        if path not in images:
            encoded += path.stat().st_size
            if encoded > 256 * 1024 * 1024:
                raise ValueError("Encoded PNG inputs exceed 256 MiB")
            with path.open("rb") as stream:
                header = stream.read(26)
            if (len(header) != 26 or header[:8] != b"\x89PNG\r\n\x1a\n"
                    or header[24:26] != b"\x08\x06"):
                raise ValueError(
                    "Use 8-bit RGBA PNGs; higher bit depths require an explicit source export")
            with Image.open(path) as image:
                if (image.format != "PNG" or image.mode != "RGBA"
                        or getattr(image, "n_frames", 1) != 1):
                    raise ValueError("Use single-image RGBA PNGs with declared straight alpha")
                width, height = image.size
                pixels += width * height
                if max(width, height) > 8192 or pixels > 32000000:
                    raise ValueError("PNG inputs exceed the texture budget")
                image.load()
            images[path] = (width, height, digest(path))
        width, height, sha = images[path]
        if pinned and (frame["sha256"] != sha or frame["width"] != width
                       or frame["height"] != height):
            raise ValueError("Frame input changed: " + str(path))
        rect = frame.get("rect", [0, 0, width, height])
        if (not isinstance(rect, list) or len(rect) != 4 or any(type(x) is not int for x in rect)
                or min(rect[:2]) < 0 or min(rect[2:]) < 1
                or rect[0] + rect[2] > width or rect[1] + rect[3] > height):
            raise ValueError("Frame rectangle is outside its PNG")
        result.append({"name": label, "path": str(path.relative_to(Path(workspace).resolve())),
                       "sha256": sha, "width": width, "height": height, "rect": rect})
    return result


def validate(workspace, receipt):
    """Check a receipt and that every frame and source file still matches it."""
    path = inside(workspace, receipt)
    data = load(path)
    if (data.get("schema") != SCHEMA or data.get("version") != 1
            or data.get("alpha") != "straight" or data.get("coordinate_origin") != "top_left"
            or type(data.get("loop")) is not bool):
        raise ValueError("Unsupported animation media receipt")
    name(data.get("name"))
    fps = rate(data.get("fps"))
    checked = frames(workspace, data.get("frames"), pinned=True)
    if len(checked) * fps["denominator"] / fps["numerator"] > 300:
        raise ValueError("Animation exceeds 300 seconds")
    sources = data.get("sources", [])
    if (not isinstance(sources, list) or len(sources) > 1066
            or any(not isinstance(item, dict) or set(item) != {"path", "sha256"}
                   for item in sources)):
        raise ValueError("Invalid source fingerprints")
    for item in sources:
        if digest(inside(workspace, item["path"])) != item["sha256"]:
            raise ValueError("Source changed: " + item["path"])
    return {"status": "inputs_unchanged", "receipt": str(path), "sha256": digest(path),
            "frame_count": len(checked), "fps": fps, "launches_applications": False}


def ingest(workspace, recipe, directory):
    """Pin explicitly ordered PNG frames or atlas rectangles and editable source files."""
    source, target = inside(workspace, recipe), inside(workspace, directory)
    spec = load(source)
    if (not isinstance(spec, dict) or spec.get("version") != 1
            or set(spec) - {"version", "name", "fps", "loop", "alpha", "frames", "sources"}
            or spec.get("alpha") != "straight" or type(spec.get("loop")) is not bool):
        raise ValueError("Declare version 1, straight alpha, loop, FPS and ordered frames")
    fps, label = rate(spec.get("fps")), name(spec.get("name"))
    checked = frames(workspace, spec.get("frames"), pinned=False)
    if len(checked) * fps["denominator"] / fps["numerator"] > 300:
        raise ValueError("Animation exceeds 300 seconds")
    sources = spec.get("sources", [])
    if (not isinstance(sources, list) or len(sources) > 64
            or any(not isinstance(item, str) for item in sources)):
        raise ValueError("Declare at most 64 source paths")
    inputs = [source, *(inside(workspace, item) for item in sources)]
    root = Path(workspace).resolve()
    refs = [{"path": str(item.relative_to(root)), "sha256": digest(item)} for item in inputs]
    target.mkdir(parents=True, exist_ok=False)
    output = target / "media.json"
    write(output, {"schema": SCHEMA, "version": 1, "name": label, "fps": fps,
                   "loop": spec["loop"], "alpha": "straight", "coordinate_origin": "top_left",
                   "frames": checked, "sources": refs})
    return validate(workspace, str(output))


def _size(directory):
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


def run(command, directory, seconds=300):
    """Run one process group in ``directory`` with bounded time, memory, log and output size.

    Writes ``execution.json`` and ``native.log`` there. Raises ``MediaOutcomeUnknown`` when
    the command fails or a limit stops it.
    """
    journal = directory / "execution.json"
    state = {"version": 1, "state": "pending",
             "arguments_sha256": hashlib.sha256(json.dumps(command).encode()).hexdigest(),
             "seconds_limit": seconds, "process_group_gib_limit": 8,
             "output_bytes_limit": MAX_OUTPUT_BYTES, "retry_safe": False}
    write(journal, state)
    start, peak, stopped = time.monotonic(), 0, None
    with (directory / "native.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            while process.poll() is None:
                stats = subprocess.run(["/bin/ps", "-axo", "pgid=,rss="], capture_output=True,
                                       text=True, check=True, timeout=3)
                rss = sum(int(fields[1]) for line in stats.stdout.splitlines()
                          if len(fields := line.split()) == 2 and int(fields[0]) == process.pid)
                peak = max(peak, rss / (1024 * 1024))
                if _size(directory) > MAX_OUTPUT_BYTES:
                    stopped = "output_limit"
                    break
                if (peak > 8 or time.monotonic() - start > seconds
                        or log.tell() > 16 * 1024 * 1024):
                    stopped = "memory" if peak > 8 else "time_or_log_limit"
                    break
                time.sleep(.1)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            state.update(state="unknown" if stopped or process.returncode else "exited",
                         exit_code=process.returncode, stopped_by_guard=stopped,
                         peak_process_group_gib=peak, elapsed_seconds=time.monotonic() - start)
            journal.write_text(json.dumps(state, indent=2) + "\n")
    if stopped or process.returncode:
        raise MediaOutcomeUnknown(
            "Native command did not finish successfully; inspect execution.json and native.log")
    if _size(directory) > MAX_OUTPUT_BYTES:
        state.update(state="unknown", stopped_by_guard="output_limit")
        journal.write_text(json.dumps(state, indent=2) + "\n")
        raise MediaOutcomeUnknown("Native output exceeded the byte budget; inspect its directory")
    return state
