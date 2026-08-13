"""ComfyUIVideoBackend — 自托管 ComfyUI（MiniMax H3 工作流）视频生成后端。

对接 server222 上已调通的 ComfyUI + MiniMax H3 工作流（机智罗 58/59 号 LoRA 加速版）。
ComfyUI 以 /prompt 提交、/history/{prompt_id} 轮询、/view 下载，与供应商 SDK 无关，
故本 backend 用裸 httpx 直连，不依赖第三方 SDK。

工作流分派：
- 58 号（MiniMaxH3ImageToVideo）：无首帧 = 文生视频（T2V），有首帧 = 图生视频（I2V），
  首尾帧同给 = 首尾帧（FL2V）。对应 ArcReel 分镜路线（start_image / end_image）。
- 59 号（MiniMaxH3ReferenceToVideo）：多参考图（ref_images）+ 独立参考音频（ref_audios）。
  对应 ArcReel 参考路线（reference_images / reference_audio_files）。

素材经 ComfyUI 的 /upload/image 上传到 input/ 目录（与 ArcReel 文件系统分离，纯 HTTP 集成），
产物经 /view 下载到 request.output_path。工作流模板存于 comfyui_workflows/*.json，
是 server222 已验证跑通的 API JSON（已剔除前端节点、解析 Reroute、清理 VHS 脏参数）。
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any

import httpx

from lib.providers import PROVIDER_COMFYUI
from lib.retry import (
    DOWNLOAD_BACKOFF_SECONDS,
    DOWNLOAD_MAX_ATTEMPTS,
    with_retry_async,
)
from lib.video_backends.base import (
    ProviderJobIdPersistenceMixin,
    ReferenceAudioMode,
    VideoCapabilities,
    VideoGenerationRequest,
    VideoGenerationResult,
    download_video,
    poll_with_retry,
    should_retry_download,
    should_retry_poll,
)

logger = logging.getLogger(__name__)

_WORKFLOWS_DIR = Path(__file__).with_name("comfyui_workflows")
_WF58_TEMPLATE = "58_i2v_fl2v.json"
_WF59_TEMPLATE = "59_ref2v.json"

# 58 号模板关键节点 id（与 comfyui_workflows/58_i2v_fl2v.json 对齐）。
_WF58_NOISE = "131"
_WF58_I2V = "133"
_WF58_PARAMS = "161"
_WF58_VHS = "168"
_WF58_FIRST_FRAME = "114"
_WF58_LAST_FRAME = "177"

# 59 号模板关键节点 id（与 comfyui_workflows/59_ref2v.json 对齐）。
_WF59_NOISE = "182"
_WF59_REF = "167"
_WF59_PARAMS = "205"
_WF59_VHS = "210"
_WF59_PROMPT = "209"

# 参考图 / 参考音频的动态 LoadImage / LoadAudio 节点 id 前缀（59 号）。
# 用纯数字字符串：ComfyUI 内部多处按 int() 解析节点 id，非数字会抛 ValueError。
_WF59_REF_IMAGE_BASE = 600
_WF59_REF_AUDIO_BASE = 800

# ArcReel 的 aspect_ratio 取值 → XB_HailuoH3VideoParams.aspect_ratio 固定枚举。
# ComfyUI 无 "adaptive" 档，未命中时回落横屏 16:9。
_ASPECT_RATIO_MAP: dict[str, str] = {
    "1:1": "1:1 (Square)",
    "2:3": "2:3 (Portrait Photo)",
    "3:2": "3:2 (Photo)",
    "3:4": "3:4 (Portrait Standard)",
    "4:3": "4:3 (Standard)",
    "9:16": "9:16 (Portrait Widescreen)",
    "16:9": "16:9 (Widescreen)",
    "21:9": "21:9 (Ultrawide)",
}
_DEFAULT_ASPECT_RATIO = "16:9 (Widescreen)"

# ArcReel 的 resolution 档位 → XB_HailuoH3VideoParams.megapixels（0.4MP 预览 / 0.9MP 出片）。
_RESOLUTION_MEGAPIXELS: dict[str, float] = {"0.4mp": 0.4, "0.9mp": 0.9}
_DEFAULT_MEGAPIXELS = 0.4

_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
}

# ComfyUI 提交/轮询/下载超时与轮询节奏。生成 5s 视频（LoRA 4 步 + Sage）实测约 140s，
# 10s 翻倍；按 120s/秒 留足余量。
_UPLOAD_TIMEOUT = 120.0
_POLL_TIMEOUT = 120.0
_POLL_INTERVAL_SECONDS = 5.0
_MIN_POLL_TIMEOUT_SECONDS = 600.0
_POLL_TIMEOUT_PER_SECOND = 120.0


def _load_template(name: str) -> dict[str, Any]:
    with open(_WORKFLOWS_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _megapixels_for(resolution: str | None) -> float:
    return _RESOLUTION_MEGAPIXELS.get((resolution or "").strip().lower(), _DEFAULT_MEGAPIXELS)


def _aspect_ratio_for(aspect_ratio: str) -> str:
    return _ASPECT_RATIO_MAP.get((aspect_ratio or "").strip().lower(), _DEFAULT_ASPECT_RATIO)


class ComfyUIVideoBackend(ProviderJobIdPersistenceMixin):
    """ComfyUI 视频后端：把 ArcReel 视频请求翻译成 58/59 工作流并异步轮询取件。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        http_timeout: float = 120.0,
    ) -> None:
        # ComfyUI 默认无鉴权（内网部署）；api_key 保留给开启了认证的实例，非空时以 Bearer 下发。
        self._api_key = api_key
        self._model = model or "MiniMax-H3"
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("ComfyUI backend 需要 base_url（ComfyUI 地址，如 http://192.168.3.222:8188）")
        self._base_url = base
        self._http_timeout = http_timeout

    @property
    def name(self) -> str:
        return PROVIDER_COMFYUI

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def video_capabilities_for_model(model: str) -> VideoCapabilities:
        """58 号承载首帧/尾帧，59 号承载多参考图（≤9）+ 独立参考音频（≤3）。"""
        return VideoCapabilities(
            first_frame=True,
            last_frame=True,
            max_reference_images=9,
            reference_audio_mode=ReferenceAudioMode.DIRECT,
            max_reference_audio_count=3,
        )

    @property
    def video_capabilities(self) -> VideoCapabilities:
        return self.video_capabilities_for_model(self._model)

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            payload = await self._build_payload(client, request)
            prompt_id = await self._submit(client, payload)
            logger.info("ComfyUI 任务已提交: prompt_id=%s model=%s", prompt_id, self._model)
            await self._persist_provider_job_id(request, prompt_id, provider=PROVIDER_COMFYUI)
            return await self._poll_and_download(client, prompt_id, request)

    async def resume_video(self, job_id: str, request: VideoGenerationRequest) -> VideoGenerationResult:
        """接续已提交的 ComfyUI 任务：仅轮询 + 下载，不重新提交（ADR 0007）。"""
        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            return await self._poll_and_download(client, job_id, request)

    # ── 素材上传 ───────────────────────────────────────────────────

    async def _upload(self, client: httpx.AsyncClient, path: Path) -> str:
        """上传本地素材到 ComfyUI input/ 目录，返回 LoadImage/LoadAudio 可引用的相对路径名。"""
        if not path.is_file():
            raise FileNotFoundError(f"ComfyUI 素材缺失: {path}")
        suffix = path.suffix.lower()
        mime = _MIME_TYPES.get(suffix, "application/octet-stream")
        filename = f"arcreel_{secrets.token_hex(8)}{suffix}"
        data = path.read_bytes()
        resp = await client.post(
            f"{self._base_url}/upload/image",
            files={"image": (filename, data, mime)},
            data={"type": "input", "overwrite": "true"},
            headers=self._headers(),
            timeout=_UPLOAD_TIMEOUT,
        )
        resp.raise_for_status()
        info = resp.json()
        name = info.get("name") or filename
        subfolder = info.get("subfolder") or ""
        return f"{subfolder}/{name}" if subfolder else name

    # ── 请求构建 ───────────────────────────────────────────────────

    async def _build_payload(self, client: httpx.AsyncClient, request: VideoGenerationRequest) -> dict[str, Any]:
        references = [r for r in (request.reference_images or []) if r]
        audios = [a for a in (request.reference_audio_files or []) if a]
        if references or audios:
            return await self._build_ref_payload(client, request, references, audios)
        return await self._build_i2v_payload(client, request)

    async def _build_i2v_payload(self, client: httpx.AsyncClient, request: VideoGenerationRequest) -> dict[str, Any]:
        """58 号：MiniMaxH3ImageToVideo。无首帧=文生，有首帧=图生，首尾帧同给=首尾帧。"""
        wf = _load_template(_WF58_TEMPLATE)
        seed = request.seed if request.seed is not None else secrets.randbelow(2**53)

        wf[_WF58_NOISE]["inputs"]["noise_seed"] = seed
        wf[_WF58_I2V]["inputs"]["prompt"] = request.prompt
        self._fill_params(wf, _WF58_PARAMS, request)
        wf[_WF58_VHS]["inputs"]["filename_prefix"] = f"ArcReel/{seed}"

        has_start = bool(request.start_image) and Path(request.start_image).is_file()
        has_end = bool(request.end_image) and Path(request.end_image).is_file()

        if has_start:
            wf[_WF58_FIRST_FRAME]["inputs"]["image"] = await self._upload(client, Path(request.start_image))  # type: ignore[arg-type]
        else:
            # 文生视频：first_frame / last_frame 是 optional，一并移除输入与 LoadImage 节点。
            wf[_WF58_I2V]["inputs"].pop("first_frame", None)
            wf.pop(_WF58_FIRST_FRAME, None)

        if has_end:
            wf[_WF58_LAST_FRAME]["inputs"]["image"] = await self._upload(client, Path(request.end_image))  # type: ignore[arg-type]
        else:
            wf[_WF58_I2V]["inputs"].pop("last_frame", None)
            wf.pop(_WF58_LAST_FRAME, None)

        return wf

    async def _build_ref_payload(
        self,
        client: httpx.AsyncClient,
        request: VideoGenerationRequest,
        references: list[Any],
        audios: list[Any],
    ) -> dict[str, Any]:
        """59 号：MiniMaxH3ReferenceToVideo。参考图 → ref_images.ref_image_*，音频 → ref_audios.ref_audio_*。"""
        wf = _load_template(_WF59_TEMPLATE)
        seed = request.seed if request.seed is not None else secrets.randbelow(2**53)

        wf[_WF59_NOISE]["inputs"]["noise_seed"] = seed
        wf[_WF59_PROMPT]["inputs"]["value"] = request.prompt
        self._fill_params(wf, _WF59_PARAMS, request)
        wf[_WF59_VHS]["inputs"]["filename_prefix"] = f"ArcReel/{seed}"

        for i, ref in enumerate(references):
            node_id = str(_WF59_REF_IMAGE_BASE + i)
            uploaded = await self._upload(client, Path(ref))
            wf[node_id] = {"class_type": "LoadImage", "inputs": {"image": uploaded}}
            wf[_WF59_REF]["inputs"][f"ref_images.ref_image_{i}"] = [node_id, 0]

        for j, audio in enumerate(audios):
            node_id = str(_WF59_REF_AUDIO_BASE + j)
            uploaded = await self._upload(client, Path(audio))
            wf[node_id] = {"class_type": "LoadAudio", "inputs": {"audio": uploaded}}
            wf[_WF59_REF]["inputs"][f"ref_audios.ref_audio_{j}"] = [node_id, 0]

        return wf

    @staticmethod
    def _fill_params(wf: dict[str, Any], params_id: str, request: VideoGenerationRequest) -> None:
        params = wf[params_id]["inputs"]
        params["aspect_ratio"] = _aspect_ratio_for(request.aspect_ratio)
        params["megapixels"] = _megapixels_for(request.resolution)
        params["duration"] = float(request.duration_seconds)
        params["fps"] = 24
        params["fps_float"] = 24.0

    # ── HTTP submit / poll / download ──────────────────────────────

    async def _submit(self, client: httpx.AsyncClient, payload: dict[str, Any]) -> str:
        resp = await client.post(
            f"{self._base_url}/prompt",
            json={"prompt": payload, "client_id": "arcreel"},
            headers=self._headers(),
            timeout=_POLL_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        errors = body.get("node_errors") or {}
        if errors:
            raise RuntimeError(f"ComfyUI 工作流节点校验失败: {errors}")
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI 提交响应缺少 prompt_id: {body}")
        return str(prompt_id)

    async def _poll_and_download(
        self,
        client: httpx.AsyncClient,
        prompt_id: str,
        request: VideoGenerationRequest,
    ) -> VideoGenerationResult:
        final = await poll_with_retry(
            poll_fn=lambda: self._poll_history(client, prompt_id),
            is_done=lambda payload: _history_completed(payload, prompt_id),
            is_failed=lambda payload: _history_failure(payload, prompt_id),
            poll_interval=_POLL_INTERVAL_SECONDS,
            max_wait=self._max_wait(request.duration_seconds),
            retry_if=should_retry_poll,
            label="ComfyUI",
            on_progress=lambda v, elapsed: logger.info("ComfyUI 视频生成中... elapsed=%ds", int(elapsed)),
        )

        filename, subfolder, media_type = _extract_output(final, prompt_id, self._model)
        url = self._view_url(filename, subfolder, media_type)
        await self._download_with_retry(url, request.output_path)
        logger.info("ComfyUI 视频下载完成: %s", request.output_path)

        return VideoGenerationResult(
            video_path=request.output_path,
            provider=PROVIDER_COMFYUI,
            model=self._model,
            duration_seconds=request.duration_seconds,
            video_uri=url,
            task_id=prompt_id,
            seed=request.seed,
            generate_audio=True,
        )

    async def _poll_history(self, client: httpx.AsyncClient, prompt_id: str) -> dict[str, Any]:
        resp = await client.get(f"{self._base_url}/history/{prompt_id}", headers=self._headers(), timeout=_POLL_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def _view_url(self, filename: str, subfolder: str, media_type: str) -> str:
        # filename/subfolder 来自 ComfyUI history，属上游受控值，仍做 URL 编码避免特殊字符破坏查询串。
        params = httpx.QueryParams({"filename": filename, "subfolder": subfolder, "type": media_type})
        return f"{self._base_url}/view?{params}"

    @staticmethod
    @with_retry_async(
        max_attempts=DOWNLOAD_MAX_ATTEMPTS,
        backoff_seconds=DOWNLOAD_BACKOFF_SECONDS,
        retry_if=should_retry_download,
    )
    async def _download_with_retry(download_url: str, output_path: Path) -> None:
        await download_video(download_url, output_path, timeout=300)

    @staticmethod
    def _max_wait(duration_seconds: int) -> float:
        return max(_MIN_POLL_TIMEOUT_SECONDS, duration_seconds * _POLL_TIMEOUT_PER_SECOND)


# ── history 解析工具 ────────────────────────────────────────────────


def _history_entry(payload: dict[str, Any], prompt_id: str) -> dict[str, Any]:
    entry = payload.get(prompt_id)
    return entry if isinstance(entry, dict) else {}


def _history_completed(payload: dict[str, Any], prompt_id: str) -> bool:
    status = _history_entry(payload, prompt_id).get("status")
    return bool(isinstance(status, dict) and status.get("completed"))


def _history_failure(payload: dict[str, Any], prompt_id: str) -> str | None:
    status = _history_entry(payload, prompt_id).get("status")
    if not isinstance(status, dict) or status.get("status_str") != "error":
        return None
    for message in status.get("messages") or []:
        if isinstance(message, list) and len(message) > 1 and message[0] == "execution_error":
            detail = message[1]
            if isinstance(detail, dict) and detail.get("exception_message"):
                return str(detail["exception_message"])[:200]
    return "ComfyUI 执行失败"


def _extract_output(payload: dict[str, Any], prompt_id: str, model: str) -> tuple[str, str, str]:
    """从 history 的 VHS 节点输出取 (filename, subfolder, type)。VHS 产在 gifs 槽（mp4）。"""
    entry = _history_entry(payload, prompt_id)
    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        raise RuntimeError(f"ComfyUI 任务 {prompt_id} 完成但缺少 outputs")
    for node_id, node_output in outputs.items():
        if not isinstance(node_output, dict):
            continue
        for slot in ("gifs", "videos", "images"):
            items = node_output.get(slot)
            if isinstance(items, list) and items:
                first = items[0]
                if isinstance(first, dict) and first.get("filename"):
                    return str(first["filename"]), str(first.get("subfolder") or ""), str(first.get("type") or "output")
    raise RuntimeError(f"ComfyUI 任务 {prompt_id} 完成但未找到视频产物（model={model}）")
