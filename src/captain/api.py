"""Bridge to DaVinci Resolve's Python scripting API.

Captain runs as an external process, so we locate and import
DaVinciResolveScript manually, then talk to the running Resolve instance.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("Captain.api")


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
    name: str
    track_type: str  # "video" | "audio"
    track_index: int
    timeline_start_frame: int
    timeline_end_frame: int
    source_start_frame: int
    source_end_frame: int
    file_path: str
    fps: float
    item: Any  # TimelineItem
    media_pool_item: Any  # MediaPoolItem or None

    @property
    def source_start_sec(self) -> float:
        return self.source_start_frame / self.fps

    @property
    def duration_sec(self) -> float:
        return (self.source_end_frame - self.source_start_frame) / self.fps


class ResolveError(RuntimeError):
    pass


class ResolveHandler:
    def __init__(self) -> None:
        self.resolve = None

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
                "Could not connect to DaVinci Resolve. Make sure Resolve is "
                "running with a project open, and that scripting is enabled in "
                "Preferences > System > General ('Local' or 'Network')."
            )
        log.info("Connected to Resolve %s", self.resolve.GetVersionString())

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

    # ---- reads ----------------------------------------------------------

    def timeline_name(self) -> str:
        return self._timeline().GetName()

    def timeline_fps(self) -> float:
        timeline = self._timeline()
        fps = timeline.GetSetting("timelineFrameRate")
        try:
            return float(fps)
        except (TypeError, ValueError):
            return float(self._project().GetSetting("timelineFrameRate") or 24)

    def list_clips(self) -> list[ClipInfo]:
        """All video and audio clips in the current timeline."""
        timeline = self._timeline()
        fps = self.timeline_fps()
        clips: list[ClipInfo] = []
        for track_type in ("video", "audio"):
            count = timeline.GetTrackCount(track_type)
            for idx in range(1, int(count) + 1):
                for item in timeline.GetItemListInTrack(track_type, idx) or []:
                    mp_item = item.GetMediaPoolItem()
                    file_path = ""
                    if mp_item is not None:
                        file_path = mp_item.GetClipProperty("File Path") or ""
                    clips.append(
                        ClipInfo(
                            name=item.GetName(),
                            track_type=track_type,
                            track_index=idx,
                            timeline_start_frame=int(item.GetStart()),
                            timeline_end_frame=int(item.GetEnd()),
                            source_start_frame=int(item.GetSourceStartFrame()),
                            source_end_frame=int(item.GetSourceEndFrame()),
                            file_path=file_path,
                            fps=fps,
                            item=item,
                            media_pool_item=mp_item,
                        )
                    )
        return clips

    # ---- playhead sync --------------------------------------------------

    def jump_to_clip_second(self, clip: ClipInfo, second_in_clip: float) -> None:
        """Move the Edit-page playhead to a media-relative time within a clip."""
        self.resolve.OpenPage("edit")
        timeline = self._timeline()
        source_offset = second_in_clip - clip.source_start_sec
        frame = clip.timeline_start_frame + int(round(source_offset * clip.fps))
        frame = max(clip.timeline_start_frame, min(frame, clip.timeline_end_frame - 1))
        timeline.SetCurrentTimecode(self._frame_to_timecode(frame, clip.fps))

    @staticmethod
    def _frame_to_timecode(frame: int, fps: float) -> str:
        fps_i = int(round(fps))
        ff = frame % fps_i
        ss = (frame // fps_i) % 60
        mm = (frame // (fps_i * 60)) % 60
        hh = frame // (fps_i * 3600)
        return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

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
        clip: ClipInfo,
        keep_ranges_frames: list[tuple[int, int]],
        new_name: str,
    ) -> bool:
        """Fallback path: build the new timeline with AppendToTimeline."""
        if clip.media_pool_item is None:
            raise ResolveError(
                f"Clip '{clip.name}' has no media pool item; cannot assemble."
            )
        media_pool = self._project().GetMediaPool()
        timeline = media_pool.CreateEmptyTimeline(new_name)
        if timeline is None:
            raise ResolveError(f"Could not create timeline '{new_name}'.")
        self._project().SetCurrentTimeline(timeline)
        entries = [
            {
                "mediaPoolItem": clip.media_pool_item,
                "startFrame": start,
                "endFrame": end,
            }
            for start, end in keep_ranges_frames
        ]
        # Append in chunks; huge single calls have been flaky in practice.
        for i in range(0, len(entries), 50):
            if not media_pool.AppendToTimeline(entries[i : i + 50]):
                log.warning("AppendToTimeline chunk %d failed", i // 50)
                return False
        return True
