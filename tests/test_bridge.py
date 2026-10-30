"""Tests for the localhost JSON-RPC Resolve bridge."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from captain.api import BridgedResolveHandler, ClipInfo, ResolveError, ResolveHandler
from captain.bridge import BridgeClient, BridgeError, BridgeServer


class FakeHost:
    """Minimal stand-in for ResolveHandler.bridge_dispatch."""

    def __init__(self):
        self.clips = [
            ClipInfo(
                clip_id="video:1:0:0",
                name="Talk",
                track_type="video",
                track_index=1,
                timeline_start_frame=0,
                timeline_end_frame=240,
                source_start_frame=0,
                source_end_frame=240,
                file_path="/tmp/talk.mov",
                fps=24.0,
            )
        ]
        self.jumps: list[tuple[str, float]] = []
        self.frame_jumps: list[int] = []
        self.imported: list[str] = []
        self.appended: list[tuple] = []
        self.replaced: list[tuple] = []
        self.created_captions: list[tuple] = []
        self.preview_paths: list[str] = []
        self.playhead_clip = self.clips[0]

    def dispatch(self, method: str, params: dict):
        if method == "ping":
            return {"ok": True, "version": "19.1 Free", "mode": "injected"}
        if method == "timeline_name":
            return "Episode 1"
        if method == "list_timeline_names":
            return ["Episode 1", "Interview [Captain] 1"]
        if method == "timeline_fps":
            return 24.0
        if method == "timeline_info":
            return {
                "fps": 24.0,
                "width": 1920,
                "height": 1080,
                "playhead_timecode": "00:00:02:12",
                "playhead_frame": 60,
            }
        if method == "capture_current_frame":
            self.preview_paths.append(params["image_path"])
            return params["image_path"]
        if method == "list_clips":
            return [c.to_dict() for c in self.clips]
        if method == "clip_under_playhead":
            return self.playhead_clip.to_dict()
        if method == "jump_to_clip_second":
            self.jumps.append((params["clip_id"], params["second_in_clip"]))
            return True
        if method == "jump_to_timeline_frame":
            self.frame_jumps.append(params["frame"])
            return True
        if method == "import_timeline_xml":
            self.imported.append(params["xml_path"])
            return True
        if method == "assemble_append":
            self.appended.append(
                (params["clip_id"], params["keep_ranges_frames"], params["new_name"])
            )
            return True
        if method == "replace_clip_in_place":
            self.replaced.append(
                (
                    params["clip_id"],
                    params["keep_ranges_frames"],
                    bool(params.get("ripple", False)),
                )
            )
            return True
        if method == "create_captions":
            self.created_captions.append(
                (params["clip_id"], params["captions"], params["settings"])
            )
            return len(params["captions"])
        raise ResolveError(f"unknown {method}")


@pytest.fixture
def bridge_pair():
    host = FakeHost()
    server = BridgeServer(host.dispatch, token="test-token")
    url = server.start()
    client = BridgeClient.from_url(url, "test-token")
    client.connect()
    yield host, server, client
    client.close()
    server.stop()


def test_auth_rejects_bad_token():
    host = FakeHost()
    server = BridgeServer(host.dispatch, token="secret")
    url = server.start()
    try:
        bad = BridgeClient.from_url(url, "wrong")
        with pytest.raises(BridgeError, match="Invalid bridge token"):
            bad.connect()
    finally:
        server.stop()


def test_ping_and_timeline(bridge_pair):
    _host, _server, client = bridge_pair
    assert client.call("ping")["version"] == "19.1 Free"
    assert client.call("timeline_name") == "Episode 1"
    assert client.call("timeline_fps") == 24.0


def test_list_clips_roundtrip(bridge_pair):
    _host, _server, client = bridge_pair
    clips = [ClipInfo.from_dict(d) for d in client.call("list_clips")]
    assert len(clips) == 1
    assert clips[0].name == "Talk"
    assert clips[0].file_path.endswith("talk.mov")
    assert clips[0].item is None


def test_bridged_handler_methods(bridge_pair):
    host, server, _client = bridge_pair
    from captain.bridge import BridgeClient

    handler = BridgedResolveHandler(BridgeClient.from_url(server.url, server.token))
    handler.connect()
    assert handler.timeline_name() == "Episode 1"
    assert handler.timeline_info()["width"] == 1920
    assert handler.timeline_info()["playhead_frame"] == 60
    assert handler.capture_current_frame("/tmp/preview.png") == "/tmp/preview.png"
    assert handler.list_timeline_names() == ["Episode 1", "Interview [Captain] 1"]
    clips = handler.list_clips()
    assert clips[0].clip_id == "video:1:0:0"
    handler.jump_to_clip_second(clips[0], 1.5)
    assert host.jumps == [("video:1:0:0", 1.5)]
    handler.jump_to_timeline_frame(240)
    assert host.frame_jumps == [240]
    assert handler.import_timeline_xml("/tmp/out.xml") is True
    assert host.imported == ["/tmp/out.xml"]
    assert handler.assemble_append(clips[0], [(0, 10), (20, 30)], "Cut") is True
    assert host.appended[0][0] == "video:1:0:0"
    assert host.appended[0][1] == [[0, 10], [20, 30]]
    playhead = handler.clip_under_playhead()
    assert playhead.clip_id == "video:1:0:0"
    assert handler.replace_clip_in_place(clips[0], [(0, 10), (40, 50)]) is True
    assert host.replaced[0] == ("video:1:0:0", [[0, 10], [40, 50]], False)
    assert handler.replace_clip_in_place(
        clips[0], [(5, 15)], ripple=True
    ) is True
    assert host.replaced[1] == ("video:1:0:0", [[5, 15]], True)
    count = handler.create_captions(
        clips[0],
        [{"text": "Hello", "start_frame": 10, "end_frame": 20}],
        {"write_on": False},
    )
    assert count == 1
    assert host.created_captions == [
        ("video:1:0:0", [{"text": "Hello", "start_frame": 10, "end_frame": 20}], {"write_on": False})
    ]
    handler.close()


def test_clip_under_playhead_bridge(bridge_pair):
    host, _server, client = bridge_pair
    host.playhead_clip = ClipInfo(
        clip_id="video:1:100:50",
        name="Under",
        track_type="video",
        track_index=1,
        timeline_start_frame=100,
        timeline_end_frame=200,
        source_start_frame=50,
        source_end_frame=150,
        file_path="/tmp/under.mov",
        fps=24.0,
    )
    data = client.call("clip_under_playhead")
    assert data["name"] == "Under"
    assert data["timeline_start_frame"] == 100


def test_method_error_propagates(bridge_pair):
    _host, _server, client = bridge_pair
    with pytest.raises(BridgeError, match="unknown boom"):
        client.call("boom")


def test_create_resolve_handler_picks_bridge(monkeypatch):
    from captain import api

    monkeypatch.setenv(api.ENV_BRIDGE_URL, "127.0.0.1:9")
    monkeypatch.setenv(api.ENV_BRIDGE_TOKEN, "tok")
    monkeypatch.delenv(api.ENV_BRIDGE_MODE, raising=False)
    handler = api.create_resolve_handler()
    assert isinstance(handler, BridgedResolveHandler)


def test_create_resolve_handler_picks_file_bridge(monkeypatch, tmp_path):
    from captain import api

    monkeypatch.setenv(api.ENV_BRIDGE_MODE, "file")
    monkeypatch.setenv(api.ENV_BRIDGE_DIR, str(tmp_path))
    monkeypatch.setenv(api.ENV_BRIDGE_TOKEN, "tok")
    handler = api.create_resolve_handler()
    assert isinstance(handler, BridgedResolveHandler)


def test_create_resolve_handler_direct_without_env(monkeypatch):
    from captain import api

    monkeypatch.delenv(api.ENV_BRIDGE_URL, raising=False)
    monkeypatch.delenv(api.ENV_BRIDGE_TOKEN, raising=False)
    monkeypatch.delenv(api.ENV_BRIDGE_MODE, raising=False)
    monkeypatch.delenv(api.ENV_BRIDGE_DIR, raising=False)
    handler = api.create_resolve_handler()
    assert isinstance(handler, ResolveHandler)


def test_clipinfo_dict_roundtrip():
    clip = ClipInfo(
        clip_id="audio:2:100:50",
        name="VO",
        track_type="audio",
        track_index=2,
        timeline_start_frame=100,
        timeline_end_frame=200,
        source_start_frame=50,
        source_end_frame=150,
        file_path="/a.wav",
        fps=48.0,
        item=object(),
        media_pool_item=object(),
    )
    restored = ClipInfo.from_dict(clip.to_dict())
    assert restored.clip_id == clip.clip_id
    assert restored.item is None
    assert restored.media_pool_item is None
    assert restored.duration_sec == pytest.approx(100 / 48.0)


class _FakeClip:
    def __init__(self, type_name):
        self.type_name = type_name

    def GetClipProperty(self, key=None):
        if key == "Type":
            return self.type_name
        return {"Type": self.type_name}


class _FakeFolder:
    def __init__(self, name, clips=(), subs=()):
        self.name = name
        self.clips = list(clips)
        self.subs = list(subs)

    def GetName(self):
        return self.name

    def GetClipList(self):
        return self.clips

    def GetSubFolderList(self):
        return self.subs


class _FakeTitleItem:
    def __init__(self, start, end):
        self.start, self.end = start, end
        self.properties = {}

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def SetProperty(self, key, value):
        self.properties[key] = value
        return True


class _FakeCaptionMediaPool:
    def __init__(self, timeline, root=None, drb_adds_template=False, offset=0):
        self.timeline = timeline
        self.root = root or _FakeFolder("Master")
        self.drb_adds_template = drb_adds_template
        self.offset = offset
        self.requests = []
        self.imported_drbs = []

    def GetRootFolder(self):
        return self.root

    def SetCurrentFolder(self, _folder):
        return True

    def ImportFolderFromFile(self, path):
        self.imported_drbs.append(path)
        if self.drb_adds_template:
            self.root.subs.append(
                _FakeFolder("Captain", clips=[_FakeClip("Fusion Title")])
            )
        return True

    def AppendToTimeline(self, infos):
        info = infos[0]
        self.requests.append(dict(info))
        start = info["recordFrame"] + self.offset
        title = _FakeTitleItem(
            start, start + (info["endFrame"] - info["startFrame"])
        )
        self.timeline.tracks[info["trackIndex"]].append(title)
        return [title]


class _FakeCaptionTimeline:
    def __init__(self, tracks):
        self.tracks = tracks
        self.deleted = []

    def GetTrackCount(self, _kind):
        return len(self.tracks)

    def GetItemListInTrack(self, _kind, index):
        return self.tracks[index]

    def GetSetting(self, key):
        return {"timelineResolutionWidth": "1920", "timelineResolutionHeight": "1080"}.get(key)

    def AddTrack(self, _kind):
        self.tracks[len(self.tracks) + 1] = []
        return True

    def DeleteClips(self, items, _ripple):
        self.deleted.extend(items)
        return True

    def DeleteTrack(self, _kind, index):
        self.tracks.pop(index, None)
        return True


def _caption_handler(timeline, media_pool):
    class Project:
        def GetCurrentTimeline(self):
            return timeline

        def GetMediaPool(self):
            return media_pool

    class ProjectManager:
        def GetCurrentProject(self):
            return Project()

    handler = ResolveHandler()
    handler.resolve = type(
        "Resolve", (), {"GetProjectManager": lambda _self: ProjectManager()}
    )()
    clip = ClipInfo(
        clip_id="video:1:0:0", name="Talk", track_type="video", track_index=1,
        timeline_start_frame=0, timeline_end_frame=100,
        source_start_frame=0, source_end_frame=100, file_path="/tmp/talk.mov", fps=24,
    )
    handler._clips[clip.clip_id] = clip
    return handler, clip


def _captain_bin_pool(timeline):
    template = _FakeClip("Fusion Title")
    root = _FakeFolder("Master", subs=[
        _FakeFolder("Nested", subs=[_FakeFolder("Captain", clips=[_FakeClip("Video"), template])])
    ])
    return _FakeCaptionMediaPool(timeline, root=root), template


def test_direct_caption_generation_uses_a_new_top_track_when_occupied():
    timeline = _FakeCaptionTimeline({1: [], 2: [_FakeTitleItem(10, 20)]})
    media_pool, template = _captain_bin_pool(timeline)
    handler, clip = _caption_handler(timeline, media_pool)

    count = handler.create_captions(
        clip,
        [{"text": "Hello", "start_frame": 10, "end_frame": 20, "write_on_end_frame": 10}],
        {"write_on": False, "position_x": 0.6, "scale_x": 1.5, "anchor_y": 0.4},
    )
    assert count == 1
    request = media_pool.requests[0]
    assert request["trackIndex"] == 3
    assert request["recordFrame"] == 10
    assert request["endFrame"] == 10
    assert request["mediaPoolItem"] is template
    assert len(timeline.tracks[2]) == 1
    title = timeline.tracks[3][0]
    assert title.properties["PositionX"] == pytest.approx(0.6)
    assert title.properties["FontSize"] == pytest.approx(96 / 1920)
    assert title.properties["ZoomX"] == pytest.approx(1.5)
    assert title.properties["AnchorPointY"] == pytest.approx(108.0)


def test_caption_generation_appends_textplus_template_from_captain_bin():
    timeline = _FakeCaptionTimeline({1: []})
    timeline.InsertFusionTitleIntoTimeline = lambda *_a: pytest.fail("seeded a title")
    media_pool, template = _captain_bin_pool(timeline)
    media_pool.ImportMedia = lambda *_a: pytest.fail("imported media")
    handler, clip = _caption_handler(timeline, media_pool)

    count = handler.create_captions(
        clip,
        [{"text": "Hello", "start_frame": 40, "end_frame": 55, "write_on_end_frame": 40}],
        {"write_on": False},
    )

    assert count == 1
    assert media_pool.imported_drbs == []
    assert media_pool.requests[0]["mediaPoolItem"] is template
    assert media_pool.requests[0]["recordFrame"] == 40
    assert media_pool.requests[0]["trackIndex"] == 1
    assert timeline.tracks[1][0].start == 40
    assert timeline.tracks[1][0].end == 55
    assert not hasattr(timeline.tracks[1][0], "imported_comp")


def test_find_caption_template_accepts_dict_type():
    handler = ResolveHandler()
    dict_type = _FakeClip({"Type": "Fusion Title"})
    dict_type.GetClipProperty = lambda key=None: {"Type": "Fusion Title"}
    pool = _FakeCaptionMediaPool(None, root=_FakeFolder("Master", subs=[
        _FakeFolder("Captain", clips=[dict_type])
    ]))
    assert handler._find_caption_template(pool) is dict_type

    only_video = _FakeCaptionMediaPool(None, root=_FakeFolder("Master", subs=[
        _FakeFolder("Captain", clips=[_FakeClip("Video")])
    ]))
    assert handler._find_caption_template(only_video) is None


def test_caption_template_imported_from_drb_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("captain.api.data_dir", lambda: tmp_path)
    drb = tmp_path / "caption-template.drb"
    drb.write_bytes(b"drb")
    timeline = _FakeCaptionTimeline({1: []})
    media_pool = _FakeCaptionMediaPool(timeline, drb_adds_template=True)
    handler, clip = _caption_handler(timeline, media_pool)

    count = handler.create_captions(
        clip,
        [{"text": "Hello", "start_frame": 40, "end_frame": 55, "write_on_end_frame": 40}],
        {"write_on": False},
    )

    assert count == 1
    assert media_pool.imported_drbs == [str(drb)]
    assert len(media_pool.requests) == 1


def test_caption_generation_errors_without_template(monkeypatch, tmp_path):
    from captain.api import NO_CAPTION_TEMPLATE_MSG

    monkeypatch.setattr("captain.api.data_dir", lambda: tmp_path)
    timeline = _FakeCaptionTimeline({1: [_FakeTitleItem(0, 100)]})
    media_pool = _FakeCaptionMediaPool(timeline)
    handler, clip = _caption_handler(timeline, media_pool)

    with pytest.raises(ResolveError) as exc:
        handler.create_captions(
            clip,
            [{"text": "Hello", "start_frame": 40, "end_frame": 55, "write_on_end_frame": 40}],
            {"write_on": False},
        )

    assert str(exc.value) == NO_CAPTION_TEMPLATE_MSG
    assert 2 not in timeline.tracks
    assert media_pool.requests == []


def test_caption_generation_rejects_misplaced_title():
    timeline = _FakeCaptionTimeline({1: []})
    media_pool, _template = _captain_bin_pool(timeline)
    media_pool.offset = 5
    handler, clip = _caption_handler(timeline, media_pool)

    with pytest.raises(ResolveError):
        handler.create_captions(
            clip,
            [{"text": "Hello", "start_frame": 40, "end_frame": 55, "write_on_end_frame": 40}],
            {"write_on": False},
        )

    assert len(timeline.deleted) == 1
    assert timeline.deleted[0].start == 45


def test_capture_current_frame_resolves_resolve_exported_filename(tmp_path):
    still = object()

    class Album:
        def __init__(self):
            self.deleted = []

        def ExportStills(self, _stills, _folder, prefix, _format):
            (tmp_path / f"{prefix}_17.png").write_bytes(b"png")
            return True

        def DeleteStills(self, stills):
            self.deleted.extend(stills)
            return True

    album = Album()

    class Timeline:
        def GrabStill(self):
            return still

    class Gallery:
        def GetCurrentStillAlbum(self):
            return album

    class Project:
        def GetCurrentTimeline(self):
            return Timeline()

    class ProjectManager:
        def GetCurrentProject(self):
            return Project()

    resolve = type(
        "Resolve",
        (),
        {
            "GetProjectManager": lambda _self: ProjectManager(),
            "GetGallery": lambda _self: Gallery(),
        },
    )()
    handler = ResolveHandler()
    handler.resolve = resolve

    requested = tmp_path / "frame.png"
    assert handler.capture_current_frame(str(requested)) == str(tmp_path / "frame_17.png")
    assert album.deleted == [still]


def test_textplus_write_on_uses_segment_local_frames_including_single_word():
    class Spline(dict):
        def __init__(self):
            super().__init__()
            self.writes = []

        def __setitem__(self, key, value):
            self.writes.append((key, value))
            super().__setitem__(key, value)

    class Tool:
        def __init__(self):
            self.Center = (0.5, 0.5)

        def SetInput(self, key, value):
            setattr(self, key, value)

    class Comp:
        def __init__(self):
            self.spline = Spline()

        def FindTool(self, _name):
            return tool

        def BezierSpline(self):
            return self.spline

    class Item:
        def __init__(self):
            self.comp = Comp()

        def GetFusionCompByIndex(self, _index):
            return self.comp

        def SetProperty(self, _key, _value):
            return False

    tool = Tool()
    item = Item()
    caption = {
        "text": "Hi",
        "start_frame": 50,
        "end_frame": 70,
        "write_on_end_frame": 50,
    }
    ResolveHandler._apply_caption_style(item, caption, {"write_on": True})
    assert item.comp.spline.writes == [(0, 0.0), (0, 1.0)]
    assert tool.Size == pytest.approx(96 / 1920)

    item.comp.spline = Spline()
    caption["write_on_end_frame"] = 60
    ResolveHandler._apply_caption_style(item, caption, {"write_on": True})
    assert item.comp.spline.writes == [(0, 0.0), (10, 1.0)]


def test_file_bridge_roundtrip(tmp_path):
    """Simulate the Lua host: poll request.json, write response.json."""
    import json
    import time

    from captain.bridge import FileBridgeClient

    token = "file-token"
    ready = tmp_path / "ready.json"
    ready.write_text('{"ok": true}')

    stop = threading.Event()

    def host():
        req = tmp_path / "request.json"
        res = tmp_path / "response.json"
        while not stop.is_set():
            if req.is_file():
                try:
                    msg = json.loads(req.read_text())
                except ValueError:
                    time.sleep(0.01)
                    continue
                try:
                    req.unlink()
                except OSError:
                    pass
                if msg.get("method") == "auth":
                    body = {"id": msg["id"], "result": {"ok": True, "protocol": 1}}
                    if msg.get("params", {}).get("token") != token:
                        body = {
                            "id": msg["id"],
                            "error": {"message": "Invalid bridge token"},
                        }
                elif msg.get("method") == "timeline_name":
                    body = {"id": msg["id"], "result": "File Timeline"}
                else:
                    body = {
                        "id": msg["id"],
                        "error": {"message": f"unknown {msg.get('method')}"},
                    }
                res.write_text(json.dumps(body))
            time.sleep(0.01)

    t = threading.Thread(target=host, daemon=True)
    t.start()
    try:
        client = FileBridgeClient(str(tmp_path), token)
        client.connect()
        assert client.call("timeline_name") == "File Timeline"
        client.close()
    finally:
        stop.set()
        t.join(timeout=2)


def test_concurrent_calls(bridge_pair):
    _host, _server, client = bridge_pair
    results = []
    errors = []

    def worker():
        try:
            results.append(client.call("timeline_name"))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors
    assert results == ["Episode 1"] * 8
