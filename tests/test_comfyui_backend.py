"""ComfyUIVideoBackend 单元测试（mock httpx，不打真实 HTTP）。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from lib.providers import PROVIDER_COMFYUI
from lib.video_backends.base import ReferenceAudioMode, VideoGenerationRequest
from lib.video_backends.comfyui import (
    ComfyUIVideoBackend,
    _aspect_ratio_for,
    _extract_output,
    _history_completed,
    _history_failure,
    _megapixels_for,
)

pytestmark = pytest.mark.unit


def _backend() -> ComfyUIVideoBackend:
    return ComfyUIVideoBackend(base_url="http://192.168.3.222:8188")


def _request(**kwargs: object) -> VideoGenerationRequest:
    defaults: dict[str, object] = {
        "prompt": "a cinematic shot",
        "output_path": Path("/tmp/out.mp4"),
        "duration_seconds": 5,
        "resolution": "0.4mp",
        "aspect_ratio": "9:16",
        "seed": 123,
    }
    defaults.update(kwargs)
    return VideoGenerationRequest(**defaults)  # type: ignore[arg-type]


def _png(path: Path) -> Path:
    path.write_bytes(b"\x89PNG\r\n")
    return path


def test_video_capabilities() -> None:
    caps = ComfyUIVideoBackend.video_capabilities_for_model("MiniMax-H3")
    assert caps.first_frame is True
    assert caps.last_frame is True
    assert caps.max_reference_images == 9
    assert caps.reference_audio_mode is ReferenceAudioMode.DIRECT
    assert caps.max_reference_audio_count == 3


def test_aspect_ratio_mapping() -> None:
    assert _aspect_ratio_for("9:16") == "9:16 (Portrait Widescreen)"
    assert _aspect_ratio_for("16:9") == "16:9 (Widescreen)"
    assert _aspect_ratio_for("1:1") == "1:1 (Square)"
    assert _aspect_ratio_for("unknown") == "16:9 (Widescreen)"
    assert _aspect_ratio_for("") == "16:9 (Widescreen)"


def test_megapixels_mapping() -> None:
    assert _megapixels_for("0.4mp") == 0.4
    assert _megapixels_for("0.9mp") == 0.9
    assert _megapixels_for("2k") == 0.4
    assert _megapixels_for(None) == 0.4


def test_backend_requires_base_url() -> None:
    with pytest.raises(ValueError):
        ComfyUIVideoBackend(base_url="")
    with pytest.raises(ValueError):
        ComfyUIVideoBackend(base_url=None)


async def test_build_i2v_text_to_video() -> None:
    backend = _backend()
    with patch.object(backend, "_upload", new=AsyncMock()) as up:
        payload = await backend._build_i2v_payload(AsyncMock(), _request())
        up.assert_not_awaited()
    # 文生：first_frame / last_frame 输入与 LoadImage 节点都被移除。
    assert "first_frame" not in payload["133"]["inputs"]
    assert "last_frame" not in payload["133"]["inputs"]
    assert "114" not in payload
    assert "177" not in payload
    assert payload["133"]["inputs"]["prompt"] == "a cinematic shot"
    assert payload["161"]["inputs"]["aspect_ratio"] == "9:16 (Portrait Widescreen)"
    assert payload["161"]["inputs"]["megapixels"] == 0.4
    assert payload["161"]["inputs"]["duration"] == 5.0
    assert payload["131"]["inputs"]["noise_seed"] == 123


async def test_build_i2v_with_start_frame(tmp_path: Path) -> None:
    backend = _backend()
    start = _png(tmp_path / "start.png")
    with patch.object(backend, "_upload", new=AsyncMock(return_value="arcreel_start.png")):
        payload = await backend._build_i2v_payload(AsyncMock(), _request(start_image=start))
    assert payload["133"]["inputs"]["first_frame"] == ["114", 0]
    assert payload["114"]["inputs"]["image"] == "arcreel_start.png"
    assert "last_frame" not in payload["133"]["inputs"]
    assert "177" not in payload


async def test_build_i2v_first_last(tmp_path: Path) -> None:
    backend = _backend()
    start = _png(tmp_path / "start.png")
    end = _png(tmp_path / "end.png")
    with patch.object(backend, "_upload", new=AsyncMock(side_effect=["s.png", "e.png"])):
        payload = await backend._build_i2v_payload(AsyncMock(), _request(start_image=start, end_image=end))
    assert payload["133"]["inputs"]["first_frame"] == ["114", 0]
    assert payload["133"]["inputs"]["last_frame"] == ["177", 0]
    assert payload["114"]["inputs"]["image"] == "s.png"
    assert payload["177"]["inputs"]["image"] == "e.png"


async def test_build_ref_payload(tmp_path: Path) -> None:
    backend = _backend()
    refs = [_png(tmp_path / "a.png"), _png(tmp_path / "b.png")]
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF")
    with patch.object(backend, "_upload", new=AsyncMock(side_effect=["a.png", "b.png", "v.wav"])):
        payload = await backend._build_ref_payload(AsyncMock(), _request(), refs, [audio])
    assert payload["167"]["inputs"]["ref_images.ref_image_0"] == ["600", 0]
    assert payload["167"]["inputs"]["ref_images.ref_image_1"] == ["601", 0]
    assert payload["167"]["inputs"]["ref_audios.ref_audio_0"] == ["800", 0]
    assert payload["600"]["class_type"] == "LoadImage"
    assert payload["601"]["class_type"] == "LoadImage"
    assert payload["800"]["class_type"] == "LoadAudio"
    assert payload["800"]["inputs"]["audio"] == "v.wav"
    assert payload["209"]["inputs"]["value"] == "a cinematic shot"


def test_history_completed() -> None:
    assert _history_completed({"p": {"status": {"completed": True}}}, "p") is True
    assert _history_completed({"p": {"status": {"completed": False}}}, "p") is False
    assert _history_completed({}, "p") is False


def test_history_failure() -> None:
    assert _history_failure({"p": {"status": {"status_str": "success"}}}, "p") is None
    assert (
        _history_failure(
            {
                "p": {
                    "status": {"status_str": "error", "messages": [["execution_error", {"exception_message": "boom"}]]}
                }
            },
            "p",
        )
        == "boom"
    )
    assert _history_failure({"p": {"status": {"status_str": "error", "messages": []}}}, "p") == "ComfyUI 执行失败"


def test_extract_output() -> None:
    entry = {"outputs": {"210": {"gifs": [{"filename": "x.mp4", "subfolder": "ArcReel/1", "type": "output"}]}}}
    assert _extract_output({"p": entry}, "p", "MiniMax-H3") == ("x.mp4", "ArcReel/1", "output")


async def test_generate_end_to_end(tmp_path: Path) -> None:
    backend = _backend()
    out = tmp_path / "out.mp4"
    request = _request(output_path=out)

    history = {
        "pid-1": {
            "status": {"status_str": "success", "completed": True, "messages": []},
            "outputs": {"168": {"gifs": [{"filename": "v.mp4", "subfolder": "", "type": "output"}]}},
        }
    }

    with (
        patch.object(backend, "_submit", new=AsyncMock(return_value="pid-1")),
        patch.object(backend, "_poll_history", new=AsyncMock(return_value=history)),
        patch.object(backend, "_download_with_retry", new=AsyncMock()) as dl,
        patch.object(backend, "_persist_provider_job_id", new=AsyncMock()),
    ):
        result = await backend.generate(request)

    assert result.video_path == out
    assert result.provider == PROVIDER_COMFYUI
    assert result.task_id == "pid-1"
    dl.assert_awaited_once()
