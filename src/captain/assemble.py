"""Build a new cut-down timeline from keep-ranges.

Primary path: generate an FCP7 XML (xmeml v4) sequence that references the
source media file with one clipitem per keep-range, then have Resolve import
it as a new timeline. Fallback path (in api.py): AppendToTimeline.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from xml.etree import ElementTree as ET
from xml.dom import minidom

from .api import ClipInfo


def seconds_to_source_frames(
    keep_ranges: list[tuple[float, float]], clip: ClipInfo
) -> list[tuple[int, int]]:
    """Convert media-relative keep-ranges (seconds, relative to the analyzed
    audio, i.e. starting at the clip's source in-point) to absolute source
    frame ranges."""
    out: list[tuple[int, int]] = []
    for start, end in keep_ranges:
        sf = clip.source_start_frame + int(round(start * clip.fps))
        ef = clip.source_start_frame + int(round(end * clip.fps))
        sf = max(clip.source_start_frame, sf)
        ef = min(clip.source_end_frame, ef)
        if ef > sf:
            out.append((sf, ef))
    return out


def _rate_element(fps: float) -> ET.Element:
    rate = ET.Element("rate")
    ntsc = abs(fps - round(fps)) > 1e-3  # 23.976, 29.97, ...
    ET.SubElement(rate, "timebase").text = str(int(round(fps)))
    ET.SubElement(rate, "ntsc").text = "TRUE" if ntsc else "FALSE"
    return rate


def _pathurl(path: str) -> str:
    return "file://localhost" + urllib.parse.quote(path)


def _file_element(file_id: str, clip: ClipInfo, full: bool) -> ET.Element:
    fe = ET.Element("file", id=file_id)
    if full:
        ET.SubElement(fe, "name").text = clip.name
        ET.SubElement(fe, "pathurl").text = _pathurl(clip.file_path)
        fe.append(_rate_element(clip.fps))
        media = ET.SubElement(fe, "media")
        if clip.track_type == "video":
            ET.SubElement(media, "video")
        audio = ET.SubElement(media, "audio")
        ET.SubElement(audio, "channelcount").text = "2"
    return fe


def _clipitem(
    item_id: str,
    file_id: str,
    clip: ClipInfo,
    record_in: int,
    src_in: int,
    src_out: int,
    media_type: str,
    first_use_of_file: bool,
) -> ET.Element:
    ci = ET.Element("clipitem", id=item_id)
    ET.SubElement(ci, "name").text = clip.name
    ET.SubElement(ci, "enabled").text = "TRUE"
    ET.SubElement(ci, "duration").text = str(src_out - src_in)
    ci.append(_rate_element(clip.fps))
    ET.SubElement(ci, "start").text = str(record_in)
    ET.SubElement(ci, "end").text = str(record_in + (src_out - src_in))
    ET.SubElement(ci, "in").text = str(src_in)
    ET.SubElement(ci, "out").text = str(src_out)
    ci.append(_file_element(file_id, clip, full=first_use_of_file))
    if media_type == "audio":
        st = ET.SubElement(ci, "sourcetrack")
        ET.SubElement(st, "mediatype").text = "audio"
        ET.SubElement(st, "trackindex").text = "1"
    return ci


def build_fcp7_xml(
    clip: ClipInfo,
    keep_ranges_frames: list[tuple[int, int]],
    sequence_name: str,
    width: int = 1920,
    height: int = 1080,
) -> str:
    """Serialize a single-source cutlist as FCP7 XML (xmeml v4)."""
    total = sum(e - s for s, e in keep_ranges_frames)

    xmeml = ET.Element("xmeml", version="4")
    seq = ET.SubElement(xmeml, "sequence")
    ET.SubElement(seq, "name").text = sequence_name
    ET.SubElement(seq, "duration").text = str(total)
    seq.append(_rate_element(clip.fps))
    media = ET.SubElement(seq, "media")

    has_video = clip.track_type == "video"
    file_id = "captain-file-1"
    item_n = 0

    if has_video:
        video = ET.SubElement(media, "video")
        fmt = ET.SubElement(video, "format")
        sc = ET.SubElement(fmt, "samplecharacteristics")
        sc.append(_rate_element(clip.fps))
        ET.SubElement(sc, "width").text = str(width)
        ET.SubElement(sc, "height").text = str(height)
        vtrack = ET.SubElement(video, "track")
        record = 0
        for sf, ef in keep_ranges_frames:
            item_n += 1
            vtrack.append(
                _clipitem(
                    f"captain-clip-{item_n}", file_id, clip,
                    record, sf, ef, "video", first_use_of_file=(item_n == 1),
                )
            )
            record += ef - sf

    audio = ET.SubElement(media, "audio")
    atrack = ET.SubElement(audio, "track")
    record = 0
    for sf, ef in keep_ranges_frames:
        item_n += 1
        atrack.append(
            _clipitem(
                f"captain-clip-{item_n}", file_id, clip,
                record, sf, ef, "audio", first_use_of_file=(item_n == 1),
            )
        )
        record += ef - sf

    rough = ET.tostring(xmeml, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="  ")
    # minidom prepends its own declaration; add the FCP doctype after it.
    lines = pretty.split("\n")
    lines.insert(1, "<!DOCTYPE xmeml>")
    return "\n".join(line for line in lines if line.strip())
