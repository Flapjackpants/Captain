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
import os
import sys
from dataclasses import dataclass
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
        frame = clip.timeline_start_frame + int(round(source_offset * clip.fps))
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
    ) -> bool:
        """Ripple-delete the clip on the current timeline and insert keep ranges
        at the same record position / track."""
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
        track_index = host_clip.track_index
        media_type = 1 if host_clip.track_type == "video" else 2

        if not timeline.DeleteClips([item], True):
            raise ResolveError(f"Failed to delete clip '{host_clip.name}' from the timeline.")

        media_pool = self._project().GetMediaPool()
        entries = []
        rf = record_frame
        for start, end in ranges:
            duration = max(0, end - start)
            entries.append(
                {
                    "mediaPoolItem": host_clip.media_pool_item,
                    "startFrame": start,
                    "endFrame": end,
                    "trackIndex": track_index,
                    "recordFrame": rf,
                    "mediaType": media_type,
                }
            )
            rf += duration

        for i in range(0, len(entries), 50):
            if not media_pool.AppendToTimeline(entries[i : i + 50]):
                log.warning("Replace AppendToTimeline chunk %d failed", i // 50)
                return False
        # Cache is stale after timeline mutation.
        self._clips.clear()
        return True

    # ---- bridge dispatch (host process) ---------------------------------

    def bridge_dispatch(self, method: str, params: dict) -> Any:
        if method == "ping":
            return {"ok": True, "version": self.version_string(), "mode": self.mode}
        if method == "timeline_name":
            return self.timeline_name()
        if method == "timeline_fps":
            return self.timeline_fps()
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
            return bool(self.replace_clip_in_place(params["clip_id"], ranges))
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

    def timeline_fps(self) -> float:
        return float(self._client.call("timeline_fps"))

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
    ) -> bool:
        clip_id = clip if isinstance(clip, str) else clip.clip_id
        return bool(
            self._client.call(
                "replace_clip_in_place",
                {
                    "clip_id": clip_id,
                    "keep_ranges_frames": [list(r) for r in keep_ranges_frames],
                },
            )
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
