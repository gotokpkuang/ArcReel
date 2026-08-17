# 开发更新记录（dev_changelogs）

> 本文件记录在本地副本 `G:\pro\arcreel\ArcReel` 上做的二次开发改动（相对上游 ArcReel/ArcReel 的增量）。
> 每次改动提交后在此追加一条，按时间倒序（最新在上）。部署目标：server222 的 `/quakerv/arcreel`。
> 上游基线：`ed71819a`（比 v0.26.0 新 34 个提交）。

---

## 2026-08-14 · `5c482136` · feat(openai): 新增 gpt-5.6 系列文本模型（luna/sol/terra）

### 背景

用户在 OpenAI 供应商「测试连接」时 API 能列出 gpt-5.6 系列模型，但「设置 → 模型选择 → 文本模型」下拉里最高只到 gpt-5.5。根因：模型下拉由 `PROVIDER_REGISTRY["openai"].models` 静态登记驱动，而「测试连接」走 OpenAI `models.list()` 动态拉取，两边不同源。

### 改动内容

在 `lib/config/registry.py` 的 openai 供应商新增三个文本模型：

| 模型 | input（$/1M tokens） | output（$/1M tokens） | capabilities |
|---|---|---|---|
| gpt-5.6-luna | $0.20 | $1.20 | text_generation, structured_output, vision |
| gpt-5.6-sol | $5.00 | $30.00 | text_generation, structured_output, vision |
| gpt-5.6-terra | $2.00 | $12.00 | text_generation, structured_output, vision |

- 定价取自 OpenAI 官方 Standard pricing 的 short-context input/output 两档；vision 声明依据官方文档「GPT-5.6 family」支持 `low/high/original/auto` 四种 detail。
- 同步更新 openai 供应商描述文案（GPT-5.6 / GPT-5.5 / GPT-5.4）。
- 前端无需改动：模型下拉（含「按用途指定模型」的简单/复杂任务分层）均由后端 registry 动态驱动。

### 涉及文件

- `lib/config/registry.py`

### 验证

- ruff check / basedpyright（0 error）/ pytest（registry、pricing、cost 共 126 个）全通过。
- 部署后容器内读回 `PROVIDER_REGISTRY['openai'].models` 确认三模型定价与能力正确。

### 部署状态

✅ 已部署 server222（2026-08-14），容器 `production-arcreel-1` healthy，其他容器未受影响。

---

## 2026-08-13 · `95f1cef6` · feat(video): 新增 ComfyUI 供应商适配器对接自托管 MiniMax H3 工作流

### 背景

server222 上已有调通的 ComfyUI + MiniMax H3 工作流（机智罗 58/59 号 LoRA 加速版），但 ArcReel 的供应商体系无法直接对接 ComfyUI API。本次为 ArcReel 内置「ComfyUI」供应商，直连 ComfyUI 的 `/prompt`、`/history`、`/upload/image`、`/view` 端点。

### 改动内容

新增 `ComfyUIVideoBackend`（`lib/video_backends/comfyui.py`），把 ArcReel 的统一视频请求 `VideoGenerationRequest` 翻译成 58/59 工作流 API JSON：

| ArcReel 请求形态 | ComfyUI 工作流 | 节点 |
|---|---|---|
| 纯文本 | 58 号（无首帧） | `MiniMaxH3ImageToVideo` |
| 首帧（分镜路线 i2v） | 58 号 | `first_frame` |
| 首尾帧 | 58 号 | `first_frame` + `last_frame` |
| 多参考图（参考路线 r2v） | 59 号 | `ref_images.ref_image_*` |
| 参考音频 | 59 号 | `ref_audios.ref_audio_*` |

- 工作流 API 模板存于 `lib/video_backends/comfyui_workflows/58_i2v_fl2v.json` / `59_ref2v.json`（server222 已验证跑通的模板，已剔除前端节点、解析 Reroute、清理 VHS 脏参数）。
- 纯 HTTP 集成：素材经 `/upload/image` 上传到 ComfyUI input/，产物经 `/history/{prompt_id}` 轮询 + `/view` 下载到本地，无需共享卷。
- 分辨率 `0.4mp/0.9mp` → `megapixels`、时长 → `duration/fps`、比例 → `aspect_ratio` 固定枚举。
- 注册链路：`lib/providers.py`（`PROVIDER_COMFYUI`）→ `lib/video_backends/__init__.py`（register_backend）→ `lib/config/registry.py`（ProviderMeta，模型 `MiniMax-H3`）→ `lib/backend_assembly/specs.py`（装配 spec）。
- 出厂默认串行（`default_concurrency={"video": 1}`），避免并发打爆单 GPU。

### 涉及文件

- `lib/video_backends/comfyui.py`（新增）
- `lib/video_backends/comfyui_workflows/58_i2v_fl2v.json`、`59_ref2v.json`（新增）
- `lib/providers.py`、`lib/video_backends/__init__.py`、`lib/config/registry.py`、`lib/backend_assembly/specs.py`
- `tests/test_comfyui_backend.py`（新增）、`tests/test_config_registry.py`（守卫同步）

### 验证

- ruff check / basedpyright（0 error）/ lint-imports（分层契约 KEPT）/ pytest 136 个相关测试全通过。
- server222 实测：文生 + 参考生视频两路提交均 `node_errors: {}`、`status: success`、产出 mp4。

### 部署状态

✅ 已部署 server222（2026-08-13），容器 healthy。配置方法：设置页 → 新增供应商 ComfyUI → Base URL 填 `http://192.168.3.222:8188`（无 API Key）。

---

## GitHub 仓库管理（2026-08-14）

本地仓库已推送到自己的 GitHub fork，remote 配置：

| remote | 地址 | 用途 |
|---|---|---|
| `origin` | `https://github.com/gotokpkuang/ArcReel.git` | 推送目标（自己的 fork） |
| `upstream` | `https://github.com/ArcReel/ArcReel.git` | 上游同步源（原仓库） |

### 上游更新同步（无冲突时）

```bash
git fetch upstream
git merge upstream/main    # 沿用本次 merge 经验：本地改动文件与上游无重叠则零冲突
git push origin main
```

若 merge 有冲突，按冲突文件逐个人工解决（我们改动的文件集中在 `lib/video_backends/comfyui*`、`lib/providers.py`、`lib/config/registry.py`、`lib/backend_assembly/specs.py`，上游改动这些文件时才需留意）。

### 向原仓库贡献（如 ComfyUI 适配器）

```bash
git checkout -b feat/comfyui-provider   # 从本地 main 开分支
# ... 提交改动 ...
git push origin feat/comfyui-provider
# 在 GitHub 上对 gotokpkuang/ArcReel 的该分支向 ArcReel/ArcReel 发起 PR
```

### 部署 server222（不变）

仍以本地 `main` 为基础：`git -c core.autocrlf=false archive --format=tar.gz -o $env:TEMP\arcreel-src.tar.gz HEAD` → 上传 `/tmp` → 解压覆盖 `/quakerv/arcreel` → 重新 patch `docker-compose.yml` → `docker compose build && docker compose up -d`。

### 安全边界（Public 仓库注意）

- 含 server222 凭据/内网信息的文档（`deploy_server222.md`、`h3_test_report.md`、`server222视频生成指南.md`、`server222部署总结.md`）通过 `.git/info/exclude` **排除**，不入公开仓库。
- `dev_changelogs.md`、`change_doc/` 内仅私网 IP 与模型信息，可公开；如未来写入真实密钥/密码，先脱敏再提交。
