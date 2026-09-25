"""Bridge to DaVinci Resolve's Python scripting API.

Two connection modes:

1. **IPC bridge (Free + Studio)** — preferred. Resolve launches
   ``scripts/Captain.py``, which holds the live ``resolve`` object and serves
   it over a localhost JSON-RPC bridge. The UI process sets
   ``CAPTAIN_BRIDGE_URL`` / ``CAPTAIN_BRIDGE_TOKEN`` and never calls
   ``scriptapp`` itself. This is required for Resolve Free (external
   ``scriptapp`` is Studio-only since 19.1).

2. **Direct scriptapp (Studio only)** — fallback when the UI is started
   outside the Scripts menu and no bridge env vars are set.
"""

from __future__ import annotations

import logging
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .transcript import frame_to_timecode

log = logging.getLogger("Captain.api")

ENV_BRIDGE_URL = "CAPTAIN_BRIDGE_URL"
ENV_BRIDGE_TOKEN = "CAPTAIN_BRIDGE_TOKEN"
ENV_BRIDGE_MODE = "CAPTAIN_BRIDGE_MODE"  # "tcp" (default) | "file"
ENV_BRIDGE_DIR = "CAPTAIN_BRIDGE_DIR"


def _module_candidates() -> list[str]:
    env = os.environ.get("RESOLVE_SCRIPT_API")
    paths = [os.path.join(env, "Modules")] if env else []
    if sys.platform == "darwin":
        paths.append(
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve"
            "/Developer/Scripting/Modules"
        )
    elif sys.platform == "win32":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        paths.append(
            os.path.join(
                program_data,
                "Blackmagic Design", "DaVinci Resolve", "Support",
                "Developer", "Scripting", "Modules",
            )
        )
    else:
        paths.append("/opt/resolve/Developer/Scripting/Modules")
        paths.append("/home/resolve/Developer/Scripting/Modules")
    return paths


def _import_resolve_script():
    try:
        import DaVinciResolveScript  # type: ignore

        return DaVinciResolveScript
    except ImportError:
        pass
    for path in _module_candidates():
        if os.path.isdir(path) and path not in sys.path:
            sys.path.append(path)
    import DaVinciResolveScript  # type: ignore

    return DaVinciResolveScript


@dataclass
class ClipInfo:
    clip_id: str
    name: str
    track_type: str  # "video" | "audio"
    track_index: int
    timeline_start_frame: int
    timeline_end_frame: int
    source_start_frame: int
    source_end_frame: int
    file_path: str
    fps: float
    item: Any = None  # TimelineItem — only on the Resolve host process
    media_pool_item: Any = None  # MediaPoolItem — only on the host

    @property
    def source_start_sec(self) -> float:
        return self.source_start_frame / self.fps

    @property
    def duration_sec(self) -> float:
        return (self.source_end_frame - self.source_start_frame) / self.fps

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "name": self.name,
            "track_type": self.track_type,
            "track_index": self.track_index,
            "timeline_start_frame": self.timeline_start_frame,
            "timeline_end_frame": self.timeline_end_frame,
            "source_start_frame": self.source_start_frame,
            "source_end_frame": self.source_end_frame,
            "file_path": self.file_path,
            "fps": self.fps,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClipInfo":
        return cls(
            clip_id=data["clip_id"],
            name=data["name"],
            track_type=data["track_type"],
            track_index=int(data["track_index"]),
            timeline_start_frame=int(data["timeline_start_frame"]),
            timeline_end_frame=int(data["timeline_end_frame"]),
            source_start_frame=int(data["source_start_frame"]),
            source_end_frame=int(data["source_end_frame"]),
            file_path=data.get("file_path") or "",
            fps=float(data["fps"]),
        )


class ResolveError(RuntimeError):
    pass


def _make_clip_id(track_type: str, track_index: int, start: int, source_start: int) -> str:
    return f"{track_type}:{track_index}:{start}:{source_start}"


class ResolveHandler:
    """Direct Resolve API host. Used by the bridge server (and Studio fallback)."""

    def __init__(self) -> None:
        self.resolve = None
        self._clips: dict[str, ClipInfo] = {}
        self.mode = "none"  # "direct" | "injected" | "bridge" | "none"

    # ---- connection -----------------------------------------------------

    def connect(self) -> None:
        try:
            drs = _import_resolve_script()
        except ImportError as e:
            raise ResolveError(
                "Could not find the DaVinci Resolve scripting module. "
                "Is Resolve installed from blackmagicdesign.com (not the App Store)?"
            ) from e
        self.resolve = drs.scriptapp("Resolve")
        if self.resolve is None:
            raise ResolveError(
                "Could not connect to DaVinci Resolve via scriptapp(). "
                "On Resolve Free, launch Captain from Workspace → Scripts → Captain "
                "(external scripting is Studio-only). On Studio, make sure Resolve "
                "is running with a project open and scripting is set to Local."
            )
        self.mode = "direct"
        log.info("Connected to Resolve %s (direct)", self.resolve.GetVersionString())

    def connect_from_object(self, resolve: Any) -> None:
        if resolve is None:
            raise ResolveError("Resolve did not inject a resolve object into the script.")
        self.resolve = resolve
        self.mode = "injected"
        try:
            version = resolve.GetVersionString()
        except Exception:
            version = "unknown"
        log.info("Connected to Resolve %s (Scripts-injected)", version)

    @property
    def connected(self) -> bool:
        return self.resolve is not None

    def _project(self):
        project = self.resolve.GetProjectManager().GetCurrentProject()
        if project is None:
            raise ResolveError("No project is open in Resolve.")
        return project

    def _timeline(self):
        timeline = self._project().GetCurrentTimeline()
        if timeline is None:
            raise ResolveError("No timeline is open in Resolve.")
        return timeline

    def _lookup_clip(self, clip: ClipInfo | str) -> ClipInfo:
        clip_id = clip if isinstance(clip, str) else clip.clip_id
        cached = self._clips.get(clip_id)
        if cached is None:
            raise ResolveError(
                f"Unknown clip id {clip_id!r}. Refresh the clip list and try again."
            )
        return cached

    # ---- reads ----------------------------------------------------------

    def version_string(self) -> str:
        try:
            return str(self.resolve.GetVersionString())
        except Exception:
            return "unknown"

    def timeline_name(self) -> str:
        return self._timeline().GetName()

    def list_timeline_names(self) -> list[str]:
        """Names of all timelines in the current project."""
        project = self._project()
        names: list[str] = []
        try:
            count = int(project.GetTimelineCount() or 0)
        except Exception:
            count = 0
        for i in range(1, count + 1):
            try:
                timeline = project.GetTimelineByIndex(i)
            except Exception:
                timeline = None
            if timeline is None:
                continue
            try:
                names.append(str(timeline.GetName()))
            except Exception:
                pass
        return names

    def timeline_fps(self) -> float:
        timeline = self._timeline()
        fps = timeline.GetSetting("timelineFrameRate")
        try:
            return float(fps)
        except (TypeError, ValueError):
            return float(self._project().GetSetting("timelineFrameRate") or 24)

    def timeline_info(self) -> dict[str, Any]:
        """Return project-sized preview geometry and the current playhead timecode."""
        project = self._project()
        timeline = self._timeline()

        def setting(key: str, default: Any) -> Any:
            try:
                return timeline.GetSetting(key) or project.GetSetting(key) or default
            except Exception:
                return default

        fps = self.timeline_fps()
        tc = ""
        try:
            tc = str(timeline.GetCurrentTimecode() or "")
        except Exception:
            pass
        playhead_frame = 0
        parts = tc.split(":")
        if len(parts) == 4:
            try:
                hh, mm, ss, ff = (int(part) for part in parts)
                playhead_frame = ((hh * 3600 + mm * 60 + ss) * int(round(fps))) + ff
            except ValueError:
                pass
        return {
            "fps": fps,
            "width": int(setting("timelineResolutionWidth", 1920)),
            "height": int(setting("timelineResolutionHeight", 1080)),
            "playhead_timecode": tc,
            "playhead_frame": playhead_frame,
        }

    def capture_current_frame(self, image_path: str) -> str | None:
        """Export the current timeline frame as a temporary PNG and remove its still."""
        timeline = self._timeline()
        still = None
        album = None
        try:
            gallery = self.resolve.GetGallery()
            album = gallery.GetCurrentStillAlbum() if gallery else None
            still = timeline.GrabStill()
            if still is None or album is None:
                return None
            path = Path(image_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            ok = album.ExportStills([still], str(path.parent), path.stem, "png")
            if not ok:
                return None
            if path.is_file():
                return str(path)
            # Resolve appends the still ID to the requested prefix.
            candidates = sorted(path.parent.glob(f"{path.stem}_*.png"))
            return str(candidates[0]) if candidates else None
        finally:
            if still is not None and album is not None:
                try:
                    album.DeleteStills([still])
                except Exception:
                    log.warning("Could not remove temporary Resolve preview still", exc_info=True)

    @staticmethod
    def _textplus_tool(item: Any) -> Any:
        try:
            comp = item.GetFusionCompByIndex(1)
        except Exception:
            return None
        if comp is None:
            return None
        for name in ("Text1", "TextPlus1", "TextPlus"):
            try:
                tool = comp.FindTool(name)
            except Exception:
                tool = None
            if tool is not None:
                return comp, tool
        return comp, None

    @classmethod
    def _apply_caption_style(
        cls,
        item: Any,
        caption: dict[str, Any],
        settings: dict[str, Any],
        *,
        project_width: int = 1920,
        project_height: int = 1080,
    ) -> None:
        """Apply the common Text+ Inspector controls to a generated title."""
        text_color = cls._hex_rgb(settings.get("text_color", "#FFFFFF"))
        outline_color = cls._hex_rgb(settings.get("outline_color", "#000000"))
        shadow_color = cls._hex_rgb(settings.get("shadow_color", "#000000"))
        props: dict[str, Any] = {
            "StyledText": caption["text"],
            "Font": settings.get("font_family") or "Arial",
            "Style": settings.get("font_style", "Regular"),
            "FontSize": float(settings.get("font_size", 96)) / max(1, project_height),
            "Tracking": float(settings.get("tracking", 1.0)),
            "LineSpacing": float(settings.get("line_spacing", 1.0)),
            "HorizontalJustification": {
                "left": "Left", "center": "Center", "right": "Right"
            }.get(str(settings.get("alignment", "center")), "Center"),
            "PositionX": float(settings.get("position_x", 0.5)),
            "PositionY": float(settings.get("position_y", 0.85)),
            "AnchorPointX": (float(settings.get("anchor_x", 0.5)) - 0.5) * project_width,
            "AnchorPointY": (0.5 - float(settings.get("anchor_y", 0.5))) * project_height,
            "ZoomX": float(settings.get("scale_x", 1.0)),
            "ZoomY": float(settings.get("scale_y", 1.0)),
            "RotationAngle": float(settings.get("rotation", 0.0)),
            "LayoutType": settings.get("layout_type", "Frame"),
            "Width": float(settings.get("layout_width", 1.0)),
            "Height": float(settings.get("layout_height", 1.0)),
            "VerticalJustification": str(
                settings.get("vertical_alignment", "center")
            ).capitalize(),
            "ColorRed": text_color[0], "ColorGreen": text_color[1],
            "ColorBlue": text_color[2], "ColorAlpha": 1.0,
            "OutlineEnabled": bool(settings.get("outline_enabled", False)),
            "OutlineRed": outline_color[0], "OutlineGreen": outline_color[1],
            "OutlineBlue": outline_color[2],
            "OutlineWidth": float(settings.get("outline_width", 2.0)),
            "OutlineAlpha": float(settings.get("outline_opacity", 1.0)),
            "ShadowEnabled": bool(settings.get("shadow_enabled", False)),
            "ShadowRed": shadow_color[0], "ShadowGreen": shadow_color[1],
            "ShadowBlue": shadow_color[2],
            "ShadowOpacity": float(settings.get("shadow_opacity", 0.65)),
            "Opacity": float(settings.get("image_opacity", 1.0)) * 100.0,
        }
        # The Resolve TimelineItem property layer exposes a subset of title
        # controls directly. Fusion inputs cover Text+ properties on builds
        # where that layer does not forward a control.
        tool_result = cls._textplus_tool(item)
        comp, tool = tool_result if tool_result else (None, None)
        aliases = {
            "FontSize": "Size",
            "PositionX": "Center",
            "PositionY": "Center",
            "RotationAngle": "Angle",
            "ColorAlpha": "Alpha1",
            "OutlineEnabled": "Enabled2",
            "OutlineRed": "Red2",
            "OutlineGreen": "Green2",
            "OutlineBlue": "Blue2",
            "OutlineWidth": "Thickness2",
            "OutlineAlpha": "Alpha2",
            "ShadowEnabled": "Enabled3",
            "ShadowRed": "Red3",
            "ShadowGreen": "Green3",
            "ShadowBlue": "Blue3",
            "ShadowOpacity": "Alpha3",
            "LayoutType": "LayoutType",
            "Width": "Width",
            "Height": "Height",
            "Opacity": "Opacity",
        }
        input_values = {
            "HorizontalJustification": {"Left": 0, "Center": 1, "Right": 2}.get(
                str(props["HorizontalJustification"]), 1
            ),
            "VerticalJustification": {"Top": 0, "Center": 1, "Bottom": 2}.get(
                str(props["VerticalJustification"]), 1
            ),
            "LayoutType": {"Point": 0, "Frame": 1}.get(
                str(props["LayoutType"]), 1
            ),
            "OutlineWidth": float(props["OutlineWidth"])
            / max(1, int(settings.get("font_size", 96))),
        }
        required = {
            "StyledText", "Font", "FontSize", "PositionX", "PositionY",
            "ColorRed", "ColorGreen", "ColorBlue", "ColorAlpha",
        }
        for key, value in props.items():
            try:
                applied = item.SetProperty(key, value)
            except Exception:
                applied = False
            if bool(applied):
                continue
            if tool is None:
                if key in required:
                    raise ResolveError(
                        f"Resolve could not apply Text+ setting '{key}' to a caption."
                    )
                continue
            input_key = aliases.get(key, key)
            try:
                if key in ("PositionX", "PositionY"):
                    center = (
                        (value, tool.Center[1])
                        if key == "PositionX"
                        else (tool.Center[0], value)
                    )
                    tool.Center = center
                else:
                    input_value = input_values.get(key, value)
                    if hasattr(tool, "SetInput"):
                        tool.SetInput(input_key, input_value)
                    else:
                        setattr(tool, input_key, input_value)
            except Exception:
                # Text+ inputs vary slightly between Resolve releases. An
                # unsupported optional shading property should not prevent
                # otherwise valid captions from being generated.
                if key in required:
                    raise ResolveError(
                        f"Resolve could not apply Text+ setting '{key}' to a caption."
                    )
                log.debug("Text+ input %s not available", input_key, exc_info=True)

        if bool(settings.get("write_on")) and (tool is None or comp is None):
            raise ResolveError("Resolve did not expose the Text+ controls needed for write-on.")
        if bool(settings.get("write_on")) and tool is not None and comp is not None:
            start = int(caption["start_frame"])
            finish = int(caption.get("write_on_end_frame", start))
            duration = max(0, finish - start)
            try:
                spline = comp.BezierSpline()
                spline[0] = 0.0
                spline[duration] = 1.0
                tool.WriteOnEnd = spline
            except Exception as e:
                raise ResolveError(f"Could not keyframe Text+ write-on: {e}") from e

    def create_captions(
        self,
        clip: ClipInfo | str,
        captions: list[dict[str, Any]],
        settings: dict[str, Any],
    ) -> int:
        """Add Text+ title clips above the selected clip on the current timeline."""
        host_clip = self._lookup_clip(clip)
        if host_clip.track_type != "video":
            raise ResolveError("Choose a video clip before creating captions.")
        if not captions:
            raise ResolveError("There are no caption segments to create.")
        timeline = self._timeline()
        try:
            project_width = int(timeline.GetSetting("timelineResolutionWidth") or 1920)
            project_height = int(timeline.GetSetting("timelineResolutionHeight") or 1080)
        except Exception:
            project_width, project_height = 1920, 1080
        add_title = getattr(timeline, "AddFusionTitleClip", None)
        if not callable(add_title):
            raise ResolveError(
                "This Resolve host does not expose Timeline.AddFusionTitleClip, which Captain "
                "needs to place Text+ titles at exact frame ranges."
            )

        track_count = int(timeline.GetTrackCount("video") or 0)
        added_track = False
        if track_count == 0:
            if not timeline.AddTrack("video"):
                raise ResolveError("Could not create a video track for captions.")
            track_count = int(timeline.GetTrackCount("video") or 1)
            added_track = True
        track_index = track_count
        overlaps = False
        for item in timeline.GetItemListInTrack("video", track_index) or []:
            try:
                item_start, item_end = int(item.GetStart()), int(item.GetEnd())
            except Exception:
                continue
            if any(
                int(c["start_frame"]) < item_end
                and item_start < int(c["end_frame"])
                for c in captions
            ):
                overlaps = True
                break
        if overlaps:
            if not timeline.AddTrack("video"):
                raise ResolveError(
                    "The top video track is occupied and Resolve could not add a new one."
                )
            track_index = int(timeline.GetTrackCount("video"))
            added_track = True

        items: list[Any] = []
        try:
            for caption in captions:
                start = int(caption["start_frame"])
                end = int(caption["end_frame"])
                if end <= start:
                    continue
                title = add_title("Text+", track_index, start, end - start)
                if title is None:
                    raise ResolveError(
                        f"Resolve could not create the caption at frame {start}."
                    )
                items.append(title)
                self._apply_caption_style(
                    title,
                    caption,
                    settings,
                    project_width=project_width,
                    project_height=project_height,
                )
            if not items:
                raise ResolveError("Resolve did not create any caption titles.")
            return len(items)
        except Exception as e:
            if items:
                try:
                    timeline.DeleteClips(items, False)
                except Exception:
                    log.error("Could not roll back partial caption insertion", exc_info=True)
            if added_track:
                try:
                    if not (timeline.GetItemListInTrack("video", track_index) or []):
                        timeline.DeleteTrack("video", track_index)
                except Exception:
                    log.warning("Could not remove empty caption track", exc_info=True)
            if isinstance(e, ResolveError):
                raise
            raise ResolveError(f"Caption generation failed: {e}") from e

    @staticmethod
    def _hex_rgb(value: Any) -> tuple[float, float, float]:
        text = str(value or "#FFFFFF").lstrip("#")
        try:
            return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
        except (ValueError, IndexError):
            return (1.0, 1.0, 1.0)

    def list_clips(self) -> list[ClipInfo]:
        """All video and audio clips in the current timeline."""
        timeline = self._timeline()
        fps = self.timeline_fps()
        clips: list[ClipInfo] = []
        self._clips.clear()
        for track_type in ("video", "audio"):
            count = timeline.GetTrackCount(track_type)
            for idx in range(1, int(count) + 1):
                for item in timeline.GetItemListInTrack(track_type, idx) or []:
                    mp_item = item.GetMediaPoolItem()
                    file_path = ""
                    if mp_item is not None:
                        file_path = mp_item.GetClipProperty("File Path") or ""
                    start = int(item.GetStart())
                    source_start = int(item.GetSourceStartFrame())
                    clip_id = _make_clip_id(track_type, idx, start, source_start)
                    clip = ClipInfo(
                        clip_id=clip_id,
                        name=item.GetName(),
                        track_type=track_type,
                        track_index=idx,
                        timeline_start_frame=start,
                        timeline_end_frame=int(item.GetEnd()),
                        source_start_frame=source_start,
                        source_end_frame=int(item.GetSourceEndFrame()),
                        file_path=file_path,
                        fps=fps,
                        item=item,
                        media_pool_item=mp_item,
                    )
                    self._clips[clip_id] = clip
                    clips.append(clip)
        return clips

    def clip_under_playhead(self) -> ClipInfo:
        """Return the video clip under the Edit-page playhead."""
        timeline = self._timeline()
        item = timeline.GetCurrentVideoItem()
        if item is None:
            raise ResolveError(
                "No video clip under the playhead. Move the playhead over a clip."
            )
        clips = self.list_clips()
        start = int(item.GetStart())
        source_start = int(item.GetSourceStartFrame())
        for clip in clips:
            if (
                clip.track_type == "video"
                and clip.timeline_start_frame == start
                and clip.source_start_frame == source_start
            ):
                if not clip.file_path:
                    raise ResolveError(
                        f"Clip '{clip.name}' has no media file path and cannot be transcribed."
                    )
                return clip
        # Fallback: build ClipInfo from the TimelineItem directly.
        track_type, track_index = "video", 1
        try:
            info = item.GetTrackTypeAndIndex()
            if info and len(info) >= 2:
                track_type, track_index = str(info[0]), int(info[1])
        except Exception:
            pass
        fps = self.timeline_fps()
        mp_item = item.GetMediaPoolItem()
        file_path = ""
        if mp_item is not None:
            file_path = mp_item.GetClipProperty("File Path") or ""
        if not file_path:
            raise ResolveError(
                f"Clip '{item.GetName()}' has no media file path and cannot be transcribed."
            )
        clip_id = _make_clip_id(track_type, track_index, start, source_start)
        clip = ClipInfo(
            clip_id=clip_id,
            name=item.GetName(),
            track_type=track_type,
            track_index=track_index,
            timeline_start_frame=start,
            timeline_end_frame=int(item.GetEnd()),
            source_start_frame=source_start,
            source_end_frame=int(item.GetSourceEndFrame()),
            file_path=file_path,
            fps=fps,
            item=item,
            media_pool_item=mp_item,
        )
        self._clips[clip_id] = clip
        return clip

    # ---- playhead sync --------------------------------------------------

    def jump_to_clip_second(self, clip: ClipInfo | str, second_in_clip: float) -> None:
        """Move the Edit-page playhead to a media-relative time within a clip."""
        clip = self._lookup_clip(clip) if isinstance(clip, str) else (
            self._clips.get(clip.clip_id) or clip
        )
        self.resolve.OpenPage("edit")
        timeline = self._timeline()
        source_offset = second_in_clip - clip.source_start_sec
        # First frame at or after onset — never round backward into pre-word silence.
        offset = int(math.ceil(source_offset * clip.fps - 1e-9))
        frame = clip.timeline_start_frame + max(0, offset)
        frame = max(clip.timeline_start_frame, min(frame, clip.timeline_end_frame - 1))
        timeline.SetCurrentTimecode(frame_to_timecode(frame, clip.fps))

    # ---- assemble -------------------------------------------------------

    def import_timeline_xml(self, xml_path: str) -> bool:
        """Import an FCP7 XML file as a new timeline inside a 'Captain' bin."""
        project = self._project()
        media_pool = project.GetMediaPool()
        root = media_pool.GetRootFolder()
        captain_bin = None
        for folder in root.GetSubFolderList() or []:
            if folder.GetName() == "Captain":
                captain_bin = folder
                break
        if captain_bin is None:
            captain_bin = media_pool.AddSubFolder(root, "Captain")
        if captain_bin is not None:
            media_pool.SetCurrentFolder(captain_bin)
        timeline = media_pool.ImportTimelineFromFile(xml_path)
        return timeline is not None

    def assemble_append(
        self,
        clip: ClipInfo | str,
        keep_ranges_frames: list[tuple[int, int]] | list[list[int]],
        new_name: str,
    ) -> bool:
        """Fallback path: build the new timeline with AppendToTimeline."""
        host_clip = self._lookup_clip(clip)
        if host_clip.media_pool_item is None:
            raise ResolveError(
                f"Clip '{host_clip.name}' has no media pool item; cannot assemble."
            )
        media_pool = self._project().GetMediaPool()
        timeline = media_pool.CreateEmptyTimeline(new_name)
        if timeline is None:
            raise ResolveError(f"Could not create timeline '{new_name}'.")
        self._project().SetCurrentTimeline(timeline)
        entries = [
            {
                "mediaPoolItem": host_clip.media_pool_item,
                "startFrame": int(start),
                "endFrame": int(end),
            }
            for start, end in keep_ranges_frames
        ]
        for i in range(0, len(entries), 50):
            if not media_pool.AppendToTimeline(entries[i : i + 50]):
                log.warning("AppendToTimeline chunk %d failed", i // 50)
                return False
        return True

    def replace_clip_in_place(
        self,
        clip: ClipInfo | str,
        keep_ranges_frames: list[tuple[int, int]] | list[list[int]],
        *,
        ripple: bool = False,
    ) -> bool:
        """Delete the clip on the current timeline and insert keep ranges
        at the same record position / track.

        When ``ripple`` is False (default), other tracks keep their timing
        and a gap may remain if the edit is shorter. When True, Resolve
        ripple-deletes and later clips on the timeline may shift.
        """
        if not self._clips:
            self.list_clips()
        host_clip = self._lookup_clip(clip)
        if host_clip.item is None or host_clip.media_pool_item is None:
            self.list_clips()
            host_clip = self._lookup_clip(clip)
        if host_clip.item is None:
            raise ResolveError(
                f"Clip '{host_clip.name}' is not available on the current timeline."
            )
        if host_clip.media_pool_item is None:
            raise ResolveError(
                f"Clip '{host_clip.name}' has no media pool item; cannot replace."
            )
        ranges = [(int(s), int(e)) for s, e in keep_ranges_frames]
        if not ranges:
            raise ResolveError("Nothing left to keep.")

        timeline = self._timeline()
        item = host_clip.item
        record_frame = int(item.GetStart())

        # Delete primary + linked A/V together, then re-insert both. Otherwise
        # ripple drops audio, and non-ripple leaves full-length audio behind.
        to_delete = [item]
        video_tracks: list[int] = []
        audio_tracks: list[int] = []

        def _add_track(track_type: str, track_index: int) -> None:
            bucket = audio_tracks if track_type == "audio" else video_tracks
            if track_index not in bucket:
                bucket.append(track_index)

        _add_track(host_clip.track_type, host_clip.track_index)

        try:
            linked = item.GetLinkedItems() or []
        except Exception:
            linked = []
        for linked_item in linked:
            for track_type in ("video", "audio"):
                count = int(timeline.GetTrackCount(track_type) or 0)
                for idx in range(1, count + 1):
                    for other in timeline.GetItemListInTrack(track_type, idx) or []:
                        if other == linked_item:
                            _add_track(track_type, idx)
                            if linked_item not in to_delete:
                                to_delete.append(linked_item)

        if not timeline.DeleteClips(to_delete, bool(ripple)):
            raise ResolveError(f"Failed to delete clip '{host_clip.name}' from the timeline.")

        media_pool = self._project().GetMediaPool()
        entries = []
        rf = record_frame
        for start, end in ranges:
            duration = max(0, end - start)
            for vidx in video_tracks:
                entries.append(
                    {
                        "mediaPoolItem": host_clip.media_pool_item,
                        "startFrame": start,
                        "endFrame": end,
                        "trackIndex": vidx,
                        "recordFrame": rf,
                        "mediaType": 1,
                    }
                )
            for aidx in audio_tracks:
                entries.append(
                    {
                        "mediaPoolItem": host_clip.media_pool_item,
                        "startFrame": start,
                        "endFrame": end,
                        "trackIndex": aidx,
                        "recordFrame": rf,
                        "mediaType": 2,
                    }
                )
            if not video_tracks and not audio_tracks:
                media_type = 2 if host_clip.track_type == "audio" else 1
                entries.append(
                    {
                        "mediaPoolItem": host_clip.media_pool_item,
                        "startFrame": start,
                        "endFrame": end,
                        "trackIndex": host_clip.track_index,
                        "recordFrame": rf,
                        "mediaType": media_type,
                    }
                )
            rf += duration

        inserted: list[Any] = []
        for i in range(0, len(entries), 50):
            appended = media_pool.AppendToTimeline(entries[i : i + 50])
            if not appended:
                log.warning("Replace AppendToTimeline chunk %d failed", i // 50)
                return False
            if isinstance(appended, list):
                inserted.extend(appended)

        if video_tracks and audio_tracks:
            self._relink_inserted(
                timeline, ranges, entries, inserted,
                video_tracks, audio_tracks, record_frame,
            )
        # Cache is stale after timeline mutation.
        self._clips.clear()
        return True

    def _relink_inserted(
        self,
        timeline: Any,
        ranges: list[tuple[int, int]],
        entries: list[dict],
        inserted: list[Any],
        video_tracks: list[int],
        audio_tracks: list[int],
        record_frame: int,
    ) -> None:
        """Re-link the A/V items created by replace_clip_in_place.

        Prefer the items AppendToTimeline returned (entries are built per keep
        range, video then audio, so slice per range). Position-based matching
        is unreliable after a ripple delete shifts timeline content, so the
        fallback scan matches by source range only and groups items by the
        timeline start where they actually landed.
        """
        group_size = len(video_tracks) + len(audio_tracks)
        if len(inserted) == len(entries):
            for g in range(0, len(inserted), group_size):
                group = inserted[g : g + group_size]
                if len(group) >= 2 and timeline.SetClipsLinked(group, True) is False:
                    log.warning(
                        "SetClipsLinked failed for inserted group %d", g // group_size
                    )
            return

        link_rf = record_frame
        for start, end in ranges:
            duration = max(0, end - start)
            by_start: dict[int, list[Any]] = {}
            for track_type, indices in (
                ("video", video_tracks),
                ("audio", audio_tracks),
            ):
                for idx in indices:
                    for item in timeline.GetItemListInTrack(track_type, idx) or []:
                        try:
                            src_start = int(item.GetSourceStartFrame())
                            src_end = int(item.GetSourceEndFrame())
                            item_start = int(item.GetStart())
                        except Exception:
                            continue
                        if src_start == start and src_end == end:
                            by_start.setdefault(item_start, []).append(item)
            candidates = [
                (s, group) for s, group in by_start.items() if len(group) >= 2
            ]
            if candidates:
                _, best_group = min(
                    candidates, key=lambda sg: abs(sg[0] - link_rf)
                )
                if timeline.SetClipsLinked(best_group, True) is False:
                    log.warning("SetClipsLinked failed for range %d-%d", start, end)
            else:
                log.warning("No linkable items found for range %d-%d", start, end)
            link_rf += duration

    # ---- bridge dispatch (host process) ---------------------------------

    def bridge_dispatch(self, method: str, params: dict) -> Any:
        if method == "ping":
            return {"ok": True, "version": self.version_string(), "mode": self.mode}
        if method == "timeline_name":
            return self.timeline_name()
        if method == "list_timeline_names":
            return self.list_timeline_names()
        if method == "timeline_fps":
            return self.timeline_fps()
        if method == "timeline_info":
            return self.timeline_info()
        if method == "capture_current_frame":
            return self.capture_current_frame(params["image_path"])
        if method == "list_clips":
            return [c.to_dict() for c in self.list_clips()]
        if method == "clip_under_playhead":
            return self.clip_under_playhead().to_dict()
        if method == "jump_to_clip_second":
            self.jump_to_clip_second(params["clip_id"], float(params["second_in_clip"]))
            return True
        if method == "import_timeline_xml":
            return bool(self.import_timeline_xml(params["xml_path"]))
        if method == "assemble_append":
            ranges = [tuple(r) for r in params["keep_ranges_frames"]]
            return bool(
                self.assemble_append(params["clip_id"], ranges, params["new_name"])
            )
        if method == "replace_clip_in_place":
            ranges = [tuple(r) for r in params["keep_ranges_frames"]]
            ripple = bool(params.get("ripple", False))
            return bool(
                self.replace_clip_in_place(params["clip_id"], ranges, ripple=ripple)
            )
        if method == "create_captions":
            return self.create_captions(
                params["clip_id"], params["captions"], params["settings"]
            )
        raise ResolveError(f"Unknown bridge method: {method}")


class BridgedResolveHandler:
    """UI-side Resolve facade that talks to the Scripts-process bridge."""

    def __init__(self, client) -> None:
        self._client = client
        self.mode = "bridge"
        self.resolve = None  # unused; kept for duck-typing

    def connect(self) -> None:
        try:
            self._client.connect()
            info = self._client.call("ping")
        except Exception as e:
            raise ResolveError(
                f"Could not connect to the Captain Resolve bridge ({e}). "
                "Launch Captain from Workspace → Scripts → Captain."
            ) from e
        log.info(
            "Connected via IPC bridge to Resolve %s",
            (info or {}).get("version", "unknown"),
        )

    @property
    def connected(self) -> bool:
        sock = getattr(self._client, "_sock", None)
        if sock is not None:
            return True
        return bool(getattr(self._client, "_connected", False))

    def version_string(self) -> str:
        return (self._client.call("ping") or {}).get("version", "unknown")

    def timeline_name(self) -> str:
        return self._client.call("timeline_name")

    def list_timeline_names(self) -> list[str]:
        return list(self._client.call("list_timeline_names") or [])

    def timeline_fps(self) -> float:
        return float(self._client.call("timeline_fps"))

    def timeline_info(self) -> dict[str, Any]:
        return dict(self._client.call("timeline_info") or {})

    def capture_current_frame(self, image_path: str) -> str | None:
        result = self._client.call("capture_current_frame", {"image_path": image_path})
        return str(result) if result else None

    def list_clips(self) -> list[ClipInfo]:
        return [ClipInfo.from_dict(d) for d in self._client.call("list_clips")]

    def clip_under_playhead(self) -> ClipInfo:
        return ClipInfo.from_dict(self._client.call("clip_under_playhead"))

    def jump_to_clip_second(self, clip: ClipInfo | str, second_in_clip: float) -> None:
        clip_id = clip if isinstance(clip, str) else clip.clip_id
        self._client.call(
            "jump_to_clip_second",
            {"clip_id": clip_id, "second_in_clip": second_in_clip},
        )

    def import_timeline_xml(self, xml_path: str) -> bool:
        return bool(self._client.call("import_timeline_xml", {"xml_path": xml_path}))

    def assemble_append(
        self,
        clip: ClipInfo | str,
        keep_ranges_frames: list[tuple[int, int]],
        new_name: str,
    ) -> bool:
        clip_id = clip if isinstance(clip, str) else clip.clip_id
        return bool(
            self._client.call(
                "assemble_append",
                {
                    "clip_id": clip_id,
                    "keep_ranges_frames": [list(r) for r in keep_ranges_frames],
                    "new_name": new_name,
                },
            )
        )

    def replace_clip_in_place(
        self,
        clip: ClipInfo | str,
        keep_ranges_frames: list[tuple[int, int]],
        *,
        ripple: bool = False,
    ) -> bool:
        clip_id = clip if isinstance(clip, str) else clip.clip_id
        return bool(
            self._client.call(
                "replace_clip_in_place",
                {
                    "clip_id": clip_id,
                    "keep_ranges_frames": [list(r) for r in keep_ranges_frames],
                    "ripple": bool(ripple),
                },
            )
        )

    def create_captions(
        self,
        clip: ClipInfo | str,
        captions: list[dict[str, Any]],
        settings: dict[str, Any],
    ) -> int:
        clip_id = clip if isinstance(clip, str) else clip.clip_id
        return int(
            self._client.call(
                "create_captions",
                {"clip_id": clip_id, "captions": captions, "settings": settings},
            )
            or 0
        )

    def close(self) -> None:
        self._client.close()


def create_resolve_handler() -> ResolveHandler | BridgedResolveHandler:
    """Pick file/TCP bridge when env vars are set; otherwise direct scriptapp."""
    token = os.environ.get(ENV_BRIDGE_TOKEN)
    mode = (os.environ.get(ENV_BRIDGE_MODE) or "").lower()
    if mode == "file" and token:
        from .bridge import FileBridgeClient

        directory = os.environ.get(ENV_BRIDGE_DIR)
        if not directory:
            raise ResolveError("CAPTAIN_BRIDGE_DIR is not set for file bridge mode.")
        return BridgedResolveHandler(FileBridgeClient(directory, token))
    url = os.environ.get(ENV_BRIDGE_URL)
    if url and token:
        from .bridge import BridgeClient

        return BridgedResolveHandler(BridgeClient.from_url(url, token))
    return ResolveHandler()
