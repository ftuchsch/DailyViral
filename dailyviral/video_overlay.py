from __future__ import annotations

import csv
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import ConfigurationError
from .http import copy_video_body, open_public_url
from .security import redact_sensitive


@dataclass(slots=True)
class OverlayRenderPlan:
    day_number: int
    local_date: date
    overlay_sentence: str
    work_dir: Path
    downloaded_video_path: Path
    overlay_text_path: Path
    output_video_path: Path


def load_overlay_messages(csv_path: Path) -> list[str]:
    if not csv_path.exists():
        raise ConfigurationError(f"Missing overlay message file: {csv_path}")

    messages: list[str] = []
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        expected = {"day_index", "overlay_sentence"}
        if set(reader.fieldnames or []) != expected:
            raise ConfigurationError(
                f"{csv_path} must define exactly these columns: day_index, overlay_sentence"
            )
        for row in reader:
            sentence = str(row.get("overlay_sentence", "")).strip()
            if sentence:
                messages.append(sentence)

    if not messages:
        raise ConfigurationError(f"No overlay messages found in {csv_path}")
    return messages


def select_overlay_sentence(*, day_number: int, csv_path: Path) -> str:
    if day_number <= 0:
        raise ConfigurationError(f"Overlay day_number must be positive, got {day_number}.")
    messages = load_overlay_messages(csv_path)
    index = (day_number - 1) % len(messages)
    return messages[index]


def build_overlay_render_plan(
    *,
    day_number: int,
    local_date: date,
    output_root: Path,
    overlay_sentence: str,
) -> OverlayRenderPlan:
    work_dir = output_root / f"day-{day_number}-{local_date.isoformat()}"
    return OverlayRenderPlan(
        day_number=day_number,
        local_date=local_date,
        overlay_sentence=overlay_sentence,
        work_dir=work_dir,
        downloaded_video_path=work_dir / "base.mp4",
        overlay_text_path=work_dir / "overlay.txt",
        output_video_path=work_dir / f"day-{day_number}.mp4",
    )


def download_video(
    *,
    url: str,
    destination: Path,
    timeout_seconds: int,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open_public_url(url, timeout_seconds=timeout_seconds) as (response, _):
        if not 200 <= response.status < 300:
            raise ConfigurationError("Base-video download returned an unsuccessful HTTP status.")
        copy_video_body(response, destination)
    return destination


def render_overlay_video(
    *,
    plan: OverlayRenderPlan,
    ffmpeg_bin: str = "ffmpeg",
    font_color: str = "white",
    margin_px: int = 30,
) -> Path:
    ffmpeg_path = shutil.which(ffmpeg_bin)
    if ffmpeg_path is None:
        raise ConfigurationError(
            "ffmpeg is required for ENABLE_VIDEO_OVERLAY=true but was not found in PATH."
        )

    plan.work_dir.mkdir(parents=True, exist_ok=True)
    plan.overlay_text_path.write_text(plan.overlay_sentence, encoding="utf-8")

    filtergraph = (
        "drawtext="
        "textfile=overlay.txt:expansion=none:"
        f"fontcolor={font_color}:"
        "fontsize=h*0.045:"
        f"x=w-tw-{margin_px}:"
        f"y=h-th-{margin_px}:"
        "box=1:"
        "boxcolor=black@0.35:"
        "boxborderw=18:"
        "shadowcolor=black@0.8:"
        "shadowx=2:"
        "shadowy=2"
    )

    try:
        result = subprocess.run(
            [
                ffmpeg_path,
                "-y",
                "-nostdin",
                "-protocol_whitelist",
                "file",
                "-f",
                "mov",
                "-i",
                plan.downloaded_video_path.name,
                "-vf",
                filtergraph,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-c:a",
                "copy",
                plan.output_video_path.name,
            ],
            cwd=plan.work_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        )
    except subprocess.TimeoutExpired:
        raise ConfigurationError("FFmpeg exceeded the five-minute render limit.") from None
    if result.returncode != 0:
        stderr = redact_sensitive((result.stderr or "").strip())
        raise ConfigurationError(
            "ffmpeg failed to render the overlay video: "
            f"{stderr or redact_sensitive(result.stdout.strip()) or 'unknown ffmpeg error'}"
        )

    if not plan.output_video_path.exists():
        raise ConfigurationError(
            f"ffmpeg completed without creating the expected file: {plan.output_video_path}"
        )
    return plan.output_video_path
