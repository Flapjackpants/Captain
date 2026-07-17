from xml.etree import ElementTree as ET

from captain.api import ClipInfo
from captain.assemble import build_fcp7_xml, seconds_to_source_frames


def make_clip(track_type="video", fps=24.0, src_start=100, src_end=2500):
    return ClipInfo(
        name="Interview A",
        track_type=track_type,
        track_index=1,
        timeline_start_frame=86400,
        timeline_end_frame=86400 + (src_end - src_start),
        source_start_frame=src_start,
        source_end_frame=src_end,
        file_path="/Volumes/Media/interview a.mov",
        fps=fps,
        item=None,
        media_pool_item=None,
    )


def test_seconds_to_source_frames_offsets_by_source_in():
    clip = make_clip()
    frames = seconds_to_source_frames([(0.0, 1.0), (2.0, 3.0)], clip)
    assert frames == [(100, 124), (148, 172)]


def test_seconds_to_source_frames_clamps():
    clip = make_clip(src_start=0, src_end=48)
    frames = seconds_to_source_frames([(-1.0, 1.0), (1.5, 99.0)], clip)
    assert frames == [(0, 24), (36, 48)]


def test_xml_video_clip_structure():
    clip = make_clip()
    xml = build_fcp7_xml(clip, [(100, 200), (300, 400)], "Cut [Captain]")
    root = ET.fromstring(xml.split("<!DOCTYPE xmeml>")[1])
    assert root.tag == "xmeml"
    seq = root.find("sequence")
    assert seq.findtext("name") == "Cut [Captain]"
    assert seq.findtext("duration") == "200"

    video_items = seq.findall("media/video/track/clipitem")
    audio_items = seq.findall("media/audio/track/clipitem")
    assert len(video_items) == 2
    assert len(audio_items) == 2

    first = video_items[0]
    assert first.findtext("in") == "100"
    assert first.findtext("out") == "200"
    assert first.findtext("start") == "0"
    assert first.findtext("end") == "100"
    second = video_items[1]
    assert second.findtext("start") == "100"
    assert second.findtext("in") == "300"

    # file defined fully once, then referenced by id
    files = root.findall(".//file")
    full = [f for f in files if f.find("pathurl") is not None]
    assert len(full) == 1
    assert " " not in full[0].findtext("pathurl")  # path is URL-quoted
    assert len({f.get("id") for f in files}) == 1


def test_xml_audio_only_clip_has_no_video_track():
    clip = make_clip(track_type="audio")
    xml = build_fcp7_xml(clip, [(0, 100)], "Audio Cut")
    root = ET.fromstring(xml.split("<!DOCTYPE xmeml>")[1])
    assert root.find("sequence/media/video") is None
    assert len(root.findall("sequence/media/audio/track/clipitem")) == 1


def test_xml_ntsc_rate():
    clip = make_clip(fps=23.976)
    xml = build_fcp7_xml(clip, [(0, 100)], "NTSC")
    root = ET.fromstring(xml.split("<!DOCTYPE xmeml>")[1])
    rate = root.find("sequence/rate")
    assert rate.findtext("timebase") == "24"
    assert rate.findtext("ntsc") == "TRUE"
