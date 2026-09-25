from __future__ import annotations

from pathlib import Path

import pytest

from captain.api import ClipInfo, ResolveError, ResolveHandler


class RenderProject:
    def __init__(self):
        self.settings = None
        self.settings_calls = []
        self.render_settings = {"SelectAllFrames": True}
        self.format_codec = {"format": "mov", "codec": "H264"}
        self.deleted = []
        self.job_path = None
        self.selected_formats = []

    def GetRenderSettings(self):
        return dict(self.render_settings)

    def GetCurrentRenderFormatAndCodec(self):
        return dict(self.format_codec)

    def SetCurrentRenderFormatAndCodec(self, fmt, codec):
        self.selected_formats.append((fmt, codec))
        self.format_codec = {"format": fmt, "codec": codec}
        return True

    def GetRenderFormats(self):
        return {"QuickTime": "mov", "Wave": "wav"}

    def GetRenderCodecs(self, render_format):
        assert render_format in {"wav", "mov"}
        return {"Linear PCM": "LinearPCM"} if render_format == "wav" else {"H.264": "H264"}

    def SetRenderSettings(self, settings):
        self.settings = dict(settings)
        self.settings_calls.append(dict(settings))
        self.render_settings = dict(settings)
        return True

    def AddRenderJob(self):
        extension = self.format_codec["format"]
        self.job_path = Path(self.settings["TargetDir"]) / f'{self.settings["CustomName"]}.{extension}'
        return "job-1"

    def StartRendering(self, _jobs, _interactive=False):
        self.job_path.write_bytes(b"wav")
        return True

    def GetRenderJobStatus(self, _job):
        return {"JobStatus": "Complete", "CompletionPercentage": 100}

    def DeleteRenderJob(self, job_id):
        self.deleted.append(job_id)
        return True


def test_render_clip_audio_renders_timeline_range_and_restores_render_setup(tmp_path):
    handler = ResolveHandler()
    clip = ClipInfo(
        clip_id="video:2:120:10",
        name="Compound",
        track_type="video",
        track_index=2,
        timeline_start_frame=120,
        timeline_end_frame=220,
        source_start_frame=10,
        source_end_frame=110,
        file_path="",
        fps=25.0,
    )
    handler._clips[clip.clip_id] = clip
    project = RenderProject()
    handler._project = lambda: project

    output = handler.render_clip_audio(clip, str(tmp_path / "captured.wav"))

    assert output == str(tmp_path / "captured.wav")
    assert Path(output).read_bytes() == b"wav"
    render_settings = project.settings_calls[0]
    assert render_settings["MarkIn"] == 120
    assert render_settings["MarkOut"] == 219
    assert render_settings["ExportVideo"] is False
    assert render_settings["ExportAudio"] is True
    assert project.deleted == ["job-1"]
    assert project.render_settings == {"SelectAllFrames": True}
    assert project.selected_formats[0] == ("wav", "LinearPCM")
    assert project.format_codec == {"format": "mov", "codec": "H264"}


def test_render_clip_audio_reports_failure_and_removes_queued_job(tmp_path):
    handler = ResolveHandler()
    clip = ClipInfo(
        clip_id="video:1:0:0", name="Fusion", track_type="video", track_index=1,
        timeline_start_frame=0, timeline_end_frame=50, source_start_frame=0,
        source_end_frame=50, file_path="", fps=25.0,
    )
    handler._clips[clip.clip_id] = clip
    project = RenderProject()
    project.StartRendering = lambda *_args: False
    handler._project = lambda: project

    with pytest.raises(ResolveError, match="could not start audio rendering"):
        handler.render_clip_audio(clip, str(tmp_path / "failed.wav"))

    assert project.deleted == ["job-1"]
    assert not (tmp_path / "failed.wav").exists()


def test_render_clip_audio_uses_audio_only_mov_when_wave_is_not_supported(tmp_path):
    handler = ResolveHandler()
    clip = ClipInfo(
        clip_id="video:1:0:0", name="Compound", track_type="video", track_index=1,
        timeline_start_frame=0, timeline_end_frame=50, source_start_frame=0,
        source_end_frame=50, file_path="", fps=25.0,
    )
    handler._clips[clip.clip_id] = clip

    class MovFallbackProject(RenderProject):
        def GetRenderFormats(self):
            # Reproduce Resolve builds that omit QuickTime despite accepting
            # the documented "mov" format ID.
            return {"Wave": "wav"}

        def GetRenderCodecs(self, render_format):
            return {}

        def SetCurrentRenderFormatAndCodec(self, fmt, codec):
            self.selected_formats.append((fmt, codec))
            if fmt == "wav":
                return False
            self.format_codec = {"format": fmt, "codec": codec}
            return True

    project = MovFallbackProject()
    handler._project = lambda: project

    output = handler.render_clip_audio(clip, str(tmp_path / "timeline-audio.wav"))

    assert output == str(tmp_path / "timeline-audio.mov")
    assert Path(output).read_bytes() == b"wav"
    assert project.selected_formats[:2] == [("wav", "LinearPCM"), ("mov", "H264")]
    assert project.settings_calls[0]["ExportVideo"] is False
    assert project.settings_calls[0]["ExportAudio"] is True
    assert "AudioCodec" not in project.settings_calls[0]
