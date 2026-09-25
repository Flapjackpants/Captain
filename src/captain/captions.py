"""Caption segment construction and Text+ settings defaults."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .api import ClipInfo
from .transcript import Transcript, media_sec_to_timeline_frame


@dataclass(frozen=True)
class CaptionSegment:
    text: str
    start_frame: int
    end_frame: int
    write_on_end_frame: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_CAPTION_SETTINGS: dict[str, Any] = {
    "word_by_word": False,
    "hold_to_next": True,
    "write_on": False,
    "auto_fit": True,
    "font_family": "",
    "font_style": "Regular",
    "font_size": 96,
    "alignment": "center",
    "vertical_alignment": "center",
    "layout_type": "Frame",
    "layout_width": 1.0,
    "layout_height": 1.0,
    "position_x": 0.5,
    "position_y": 0.85,
    "anchor_x": 0.5,
    "anchor_y": 0.5,
    "scale_x": 1.0,
    "scale_y": 1.0,
    "link_scale": True,
    "rotation": 0.0,
    "tracking": 1.0,
    "line_spacing": 1.0,
    "text_color": "#FFFFFF",
    "outline_enabled": False,
    "outline_color": "#000000",
    "outline_width": 2.0,
    "outline_opacity": 1.0,
    "shadow_enabled": False,
    "shadow_color": "#000000",
    "shadow_opacity": 0.65,
    "image_opacity": 1.0,
}


def normalize_caption_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Return defaults overlaid with persisted user settings."""
    out = dict(DEFAULT_CAPTION_SETTINGS)
    if settings:
        out.update(settings)
    out["font_size"] = max(1, int(out.get("font_size", 96)))
    for key in ("position_x", "position_y", "anchor_x", "anchor_y"):
        out[key] = min(1.0, max(0.0, float(out[key])))
    for key in ("scale_x", "scale_y"):
        out[key] = max(0.01, float(out[key]))
    out["link_scale"] = bool(out.get("link_scale", True))
    if out["link_scale"]:
        out["scale_y"] = out["scale_x"]
    for key in ("layout_width", "layout_height"):
        out[key] = min(2.0, max(0.01, float(out[key])))
    out["rotation"] = float(out["rotation"])
    out["tracking"] = float(out["tracking"])
    out["line_spacing"] = float(out["line_spacing"])
    for key in ("outline_opacity", "shadow_opacity", "image_opacity"):
        out[key] = min(1.0, max(0.0, float(out[key])))
    out["outline_width"] = max(0.0, float(out["outline_width"]))
    return out


def build_caption_segments(
    transcript: Transcript,
    clip: ClipInfo,
    *,
    word_by_word: bool = False,
    hold_to_next: bool = True,
) -> list[CaptionSegment]:
    """Build timeline-frame caption clips from current transcript edits.

    Manual boundaries split the Whisper/pause-grouped lines. Removed words are
    omitted. In word-by-word mode each kept word is a caption segment.
    """
    groups: list[list[int]] = []
    if word_by_word:
        groups = [[i] for i in transcript.order if i not in transcript.removed]
    else:
        for line in transcript.lines():
            group = [i for i in line.word_indices if i not in transcript.removed]
            if group:
                groups.append(group)

    if not groups or clip.fps <= 0:
        return []

    clip_end = clip.timeline_end_frame
    duration = max(0.0, min(transcript.duration, clip.duration_sec))
    output: list[CaptionSegment] = []
    for position, group in enumerate(groups):
        first = transcript.words[group[0]]
        last = transcript.words[group[-1]]
        start_frame = media_sec_to_timeline_frame(
            first.start, clip.timeline_start_frame, clip.fps
        )
        if hold_to_next and position + 1 < len(groups):
            next_start = transcript.words[groups[position + 1][0]].start
            # The default is a true hold through silence until the next
            # segment's onset. Clamp pathological overlaps to a one-frame
            # visible title rather than allowing zero-length clips.
            end_sec = max(first.start + 1.0 / clip.fps, next_start)
        elif hold_to_next:
            end_sec = duration
        else:
            end_sec = last.end
        end_frame = media_sec_to_timeline_frame(
            min(duration, end_sec), clip.timeline_start_frame, clip.fps
        )
        last_word_start = media_sec_to_timeline_frame(
            last.start, clip.timeline_start_frame, clip.fps
        )
        start_frame = max(clip.timeline_start_frame, start_frame)
        end_frame = min(clip_end, end_frame)
        if end_frame <= start_frame and start_frame < clip_end:
            end_frame = min(clip_end, start_frame + 1)
        if start_frame >= clip_end or end_frame <= start_frame:
            continue
        text = " ".join(transcript.words[i].text.strip() for i in group).strip()
        if not text:
            continue
        output.append(
            CaptionSegment(
                text=text,
                start_frame=start_frame,
                end_frame=end_frame,
                write_on_end_frame=min(end_frame, max(start_frame, last_word_start)),
            )
        )
    return output
