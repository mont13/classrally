#!/usr/bin/env python3
"""Measure the loudness of the music in static/audio/ so the projector plays every track equally loud.

Writes static/audio/levels.json ({"file.mp3": {"lufs": -15.3}, ...}). The
server turns it into a playback gain per track (see list_audio_tracks), so a
quiet track is not followed by one 10 dB louder when the music changes.
Needs ffmpeg (EBU R128 integrated loudness). Files too short to measure
(sound effects under a second) are left out and play unchanged.

    python3 measure_audio_levels.py [AUDIO_DIR]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

AUDIO_EXT = {".mp3", ".ogg", ".wav", ".m4a", ".aac"}


def integrated_lufs(path: Path) -> float | None:
    result = subprocess.run(
        ["ffmpeg", "-nostats", "-hide_banner", "-i", str(path), "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
        capture_output=True, text=True)
    summary = result.stderr.rsplit("Summary:", 1)[-1]
    match = re.search(r"I:\s+(-?\d+(?:\.\d+)?)\s+LUFS", summary)
    if not match:
        return None
    value = float(match.group(1))
    return value if -60.0 < value < 0.0 else None  # -70 = too short to measure


def main() -> int:
    audio_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "static" / "audio"
    levels = {}
    for path in sorted(audio_dir.iterdir()):
        if path.suffix.lower() not in AUDIO_EXT or not path.is_file():
            continue
        lufs = integrated_lufs(path)
        print(f"{path.name:40s} {'-' if lufs is None else f'{lufs:.1f} LUFS'}")
        if lufs is not None:
            levels[path.name] = {"lufs": round(lufs, 1)}
    out = audio_dir / "levels.json"
    out.write_text(json.dumps(levels, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(levels)} tracks -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
