# ComfyUI Agnes AI 开发文档

> 随开发进度实时更新。最近更新：2026-09-28（v0.5.4 图片自动落盘 / 排队进度估算 / 面板上传前提示）

## 1. 项目概述

为 ComfyUI 开发接入 **Agnes AI**（[文档](https://agnes-ai.cn/zh-Hans/docs/overview)）多模态生成 API 的自定义节点。按用户需求收敛为 **2 个节点**，对外名称统一用 **Agnes**（仅 API 协议值——模型 ID、服务域名——保持服务端原样小写）：

- `AgnesImageGenerate`（Agnes 图片生成）：文生图 / 图生图 / 多图合成 → IMAGE
- `AgnesVideoGenerate`（Agnes 视频生成）：文生视频 / 图生视频 / 首尾帧 / 全能参考 → VIDEO

不做文本 LLM 节点。

## 2. API 调研结论（2026-09-26，以 wiki.agnes-ai.cn 为准）

### 2.1 通用

- 国内站 Base URL `https://api.agnes-ai.cn/v1`；国际站 `https://apihub.agnes-ai.com/v1`
- 认证：`Authorization: Bearer <API_KEY>`（Agnes AI 开发者控制台获取）

### 2.2 图像（`POST /v1/images/generations`）

- 模型：`agnes-image-2.5-flash`（推荐）/ `2.1-flash` / `2.0-flash`，当前免费
- `size` 档位 `1K/2K/3K/4K` × `ratio` 8 种宽高比；无 seed 参数
- 图生图/多图合成：`extra_body.image` 数组（公共 URL 或 Data URI Base64）
- **坑**：`response_format` 必须放 `extra_body` 内，放顶层会出错；图生图不需要 `tags`
- 返回 `data[0].b64_json` 或 `data[0].url`

### 2.3 视频（`POST /v1/videos`，异步任务）

- **V2.0 已于 2026-09-25 下线**，全部按 2.5 系列实现：
  - `agnes-video-2.5`：全功能（参考图 ≤8、音频 ≤3、视频参考 ≤1）
  - `agnes-video-2.5-flash`：仅 720P、参考图 ≤5、**不支持视频参考**、当前限免
- `mode`：`text` / `keyframe`（first_frame/last_frame 至少一个）/ `reference`（全能参考）
- `seconds` 字符串 "4"–"12"；**`num_frames`/`frame_rate` 已废弃**（V2.0 参数，2.5 传了会 400）
- `size`：720P/1080P/1K/2K；`aspect_ratio`：16:9/9:16/1:1/4:3/3:4/21:9
- 轮询：`GET {域名根}/agnesapi?video_id=<ID>&model_name=<model>`（**不在 /v1 下**），1–2s 间隔
  - 状态机：queued / in_progress / completed / failed
  - 完成后 mp4 地址在**响应顶层 `url`**（V2.0 时代在 `metadata.url`，代码做了两处兼容）
- 定价：720P ¥0.15/s、1080P/1K ¥0.25/s、2K ¥0.35/s
- **待实测**：视频 API 文档只写"媒体 URL 需可公开访问"，未明确是否接受 Base64 Data URI；
  节点本地图片一律转 Data URI 直传，若服务端拒绝会在真机联调时确认并调整

## 3. 设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 节点数量 | 2 个（图像/视频各一） | 用户要求；模式自动判断降低使用门槛 |
| 命名 | 节点与文案统一用 Agnes；仅模型 ID、服务域名等 API 协议值保持服务端原样小写 | 用户要求更名（v0.1.1/v0.2.6）；协议值属接口契约不可改 |
| 模式判断 | 按 optional 输入连接情况推断（keyframe/reference 互斥报错） | 与用户描述一致：只连首帧=图生视频，首尾都连=首尾帧 |
| 提示词输入 | multiline STRING widget（可右键转输入口） | 用户要求外部节点接入提示词；官方 CLIP Text Encode 输出 CONDITIONING 编码向量、无法还原文本，故用 STRING 口（与官方节点 text 字段同款交互） |
| 图像返回 | `b64_json` 模式 | 省一次 URL 下载；`url` 分支也做了兼容 |
| 图片留存 | 生成后自动落盘到 `output/AgnesAI` 并输出相对路径（`saved_path`） | 免去必须再接 SaveImage 才能留存；走官方 `get_save_image_path` 自带递增序号；写盘失败只告警不影响出图 |
| 视频输出 | ComfyUI 原生 VIDEO 类型（`VideoFromFile`） | 可直连官方 SaveVideo / GetVideo Components；多路径 import 兼容旧版 |
| 本地参考图 | IMAGE tensor → PNG → `data:image/png;base64,...` | 图像 API 文档明确支持；单张按 11MB 收口（防 base64 膨胀后超 15MB） |
| HTTP 实现 | 同步 requests + 429/5xx/网络错误退避重试 | 简单直接；V3 async 生态对第三方尚不稳 |
| 进度反馈 | ComfyUI ProgressBar（`update_absolute` 绝对值语义）；服务端未给 `progress` 时按等待时长估算 0–30% | 与 ComfyUI 任务进度一致；排队阶段进度条不再长时间停在 0，且只上报递增值不回退 |
| 出站安全 | 所有 URL（base_url/参考媒体 URL/下载 URL）经 `assert_public_http_url` 校验 | 仅 http/https；拒绝环回/私网/保留地址，防 SSRF |
| 临时文件 | `tempfile.mkstemp` + 目录 realpath 校验 | 杜绝路径拼接注入 |

## 4. 参考项目（来源与借鉴点）

按约定优先参考了 GitHub 同类高星项目的成熟做法：

1. **Comfy-Org/ComfyUI 官方源码**（comfy_api_nodes/nodes_kling.py、comfy_extras/nodes_video.py、comfy_api/input_impl）
   - 借鉴：`VIDEO` 资产构造（`VideoFromFile`）、异步任务轮询 → 下载 URL → VIDEO 输出的完整链路、optional 输入互斥校验的报错文案风格
2. **starsFriday/ComfyUI-KLingAI-OmniVideo**（可灵 O1 API 节点）
   - 借鉴：pyproject `[tool.comfy]` 注册格式、requests 同步轮询 + 超时控制、IMAGE tensor 转 PNG Base64 的编码链路

## 5. 模块说明

```
nodes/agnes_api.py   AgnesClient：_request（重试/错误映射，AgnesApiError 携带 status）
                     · generate_image · create_video · poll_video（自适应退避：429/5xx/网络抖动
                       间隔 3s→…→30s 继续等待；轮询端点从 base_url 剥掉 /v1 拼域名根 /agnesapi；
                       排队期无 progress 字段时按等待时长估算 0–30% 上报，仅递增不回退）
nodes/agnes_image.py AgnesImageGenerate（V3）：8 个参考图口合并 → 自动压缩 → payload → b64 → IMAGE tensor
                     · _save_png 自动落盘 output/AgnesAI（官方 get_save_image_path 递增序号，
                       失败只告警）· 输出 image + saved_path
nodes/agnes_video.py AgnesVideoGenerate（V3）：DynamicCombo 三模式（文生/图生·首尾帧/全能参考）
                     · flash 约束（降级 720P/拒绝视频参考）· payload 组装 · 轮询（ProgressBar）· mp4 下载 → VIDEO
                     · execute 为 async，同步流程经 asyncio.to_thread 进线程池
nodes/utils.py       resolve_api_key（环境变量兜底）· tensor↔PNG base64 · assert_public_http_url
                     · download_file_to_bytes / download_video_to_temp · video_from_file（3 级兼容导入）· make_progress
web/agnes_media.js   素材面板前端扩展：隐藏 media_files widget + DOM 上传区（多选/拖入/预览/移除/清空）
                     · 按 media_mode 置灰（widget.callback + onDrawForeground 兜底）· 上传前提示跳过/超限
__init__.py          AgnesExtension + comfy_entrypoint（V3 注册；不可再提供 NODE_CLASS_MAPPINGS）
tests/dryrun_test.py 无网络干跑测试（stub torch/folder_paths/comfy_api.latest/网络层）
tests/panel_test.mjs 素材面板前端逻辑测试（stub DOM/app/api + 改写 import 后动态导入）
```

## 6. 进度记录

- **2026-09-26**
  - 完成 API 调研（overview/quickstart/image 2.5&2.1&2.0/video 2.5/2.5-flash/pricing），确认 V2.0 下线、全能参考=reference 模式
  - 完成 2 个节点 + API 客户端 + 工具层；44 项无网络干跑测试全部通过（payload 组装、模式判断、互斥校验、flash 限制、b64 编解码、安全校验）
  - README / 示例工作流 / 本开发文档完成
  - v0.1.1：按用户要求节点更名为 Agnes（类名 `Agnes*`、节点 ID `AgnesImageGenerate`/`AgnesVideoGenerate`、显示名 "Agnes 图片生成"/"Agnes 视频生成"、模块文件 `Agnes_*.py`、包目录 `ComfyUI-AgnesAI`）
  - v0.1.2：修复 fake-ip 代理环境误拦——Clash/Surge 等把域名解析为 198.18.0.0/15 虚拟 IP（RFC2544 段，Python `is_reserved` 会命中），`assert_public_http_url` 现放行该段（不指向真实内网，无 SSRF 风险），环回/私网/CGNAT 等仍拒绝；测试新增 3 个用例
  - v0.1.3：fake-ip 豁免扩展到 IPv6 `2001:2::/48`（RFC5180 benchmarking 段，用户的代理对 IPv6 解析同样返回虚拟 IP）；豁免列表收敛为 `_PROXY_FAKEIP_NETWORKS`，标准内网判断（is_private/is_reserved 等组合）保持不变；测试新增 IPv6 放行/拒绝用例
  - v0.2.0（用户真机反馈驱动）：
    - **429 修复**：Agnes 查询接口限频比文档建议（1–2s）更严，轮询遇 429 直接失败。`AgnesApiError` 携带 `status`；`poll_video` 改为自适应退避（遇 429/5xx/网络抖动间隔 3s→6s→12s→24s→30s 继续等待，不终止任务），默认轮询间隔 2s→3s
    - **V3 迁移**：ComfyUI 加载器 `load_custom_node` 在包同时有 `NODE_CLASS_MAPPINGS` 和 `comfy_entrypoint` 时只认前者（源码确认二选一），故整包迁 V3：`define_schema` + `async execute` + `asyncio.to_thread` 包同步调用（避免阻塞事件循环）；`__init__.py` 改为 `AgnesExtension(comfy_entrypoint)` 注册
    - **视频节点模式切换**：`IO.DynamicCombo`（官方 Kling 节点同款机制）实现「文生视频 / 图生视频·首尾帧 / 全能参考」三档下拉，媒体输入口随模式显隐；模式不再自动判断（keyframe 无帧图时前置报错）
    - **图像节点多图口**：ComfyUI 单输入口只能连一条线（用户反馈只能连一张图），改为 4 个可选输入口 `image`/`image_2`/`image_3`/`image_4`，每口支持 batch 多帧，合并后截断到 8 张
    - 测试重写：stub `comfy_api.latest`（IO/Schema/NodeOutput/DynamicCombo.Option）+ `typing_extensions`，`asyncio.run` 调 execute；全部通过
  - v0.2.1（用户真机反馈：33MB 参考图超 15MB 限制）：
    - `encode_frame_auto` 渐进压缩管线取代超限即报错：PNG 直出（无损）→ JPEG q95/4:4:4 原分辨率（视觉无损、保住全部像素）→ 逐级降质/降采样（q95/3072 → q92/3072 → q92/2048 → q88/1536 → q85/1024），每步达标即返回，全档超限才报错
    - Data URI mime 动态（image/png 或 image/jpeg）；RGBA 自动合成白底后转 JPEG；降采样用 LANCZOS
    - 图像节点 4 个参考图口与视频节点首帧/尾帧/参考图共用同一编码链路，一并受益；控制台打印压缩前后大小
    - 常量 `MAX_REF_PNG_BYTES` 更名 `MAX_REF_IMAGE_BYTES`（仍为 11MB，对应 base64 后 <15MB）
  - v0.2.2（用户反馈：说明支持 8 张但只有 4 个输入口）：参考图输入口扩为 `image`~`image_8` 共 8 个，与 API 8 张上限对齐；每口仍支持 batch 多帧；测试更新 schema 断言与多口用例
  - v0.2.3（用户真机反馈 `type object 'VideoFromFile' has no attribute 'InputImpl'`）：
    - 根因：`video_from_file` 兼容链的取值顺序写反（先取 attr 再从 attr 上取 ns），且只捕 ImportError——新版 `comfy_api.latest` 顶层直接导出 `VideoFromFile`，走了错误分支抛 AttributeError
    - 修复：候选改为「顶层直接导出 → InputImpl 命名空间 → 旧版 shim」，取值顺序 mod → ns → attr，捕获 (ImportError, AttributeError)
    - 教训：此前干跑测试把 `video_from_file` stub 掉、真实链路从未执行——补 3 个真实调用链测试（顶层直调 / 命名空间 fallback / 全缺失报错）
  - v0.2.4（双子 agent 代码审查驱动，P1/P2 全修 + 测试补栏）：
    - P1 安全（utils.py）：①下载禁用 requests 自动重定向，改为手动跟跳并在每跳重新公网校验（防 302 绕过 SSRF 防护打内网/云元数据）；②IPv4-mapped IPv6（::ffff:x.x.x.x）解包后再判内网（防未打 CVE-2024-4032 补丁的 Python 漏判）；③DNS rebinding TOCTOU 残余窗口在代码注释声明
    - P2 功能：①进度条改 `update_absolute` 绝对值语义（此前 update 增量语义会把累计百分比累加，任务过半即假满 100%）；②`create_video` 取消自动重试并提示控制台核对（防网关 502 下重复建任务、按秒重复计费）；③轮询 200+非 JSON（网关挑战页）按瞬时故障退避重查；④keyframe 收到 0 帧 batch 时友好报错；⑤未知模式标签直接报错（防旧工作流静默按文生视频计费）；⑥图像节点按口配额截断后再编码（大 batch 不再白烧压缩）；⑦reference 模式 images/audios 为空时不发送字段（防 400）；⑧下载失败清理半截残留文件
    - P3：压缩收口 11MiB→10.5MiB（base64 后约 14MiB < 15 十进制 MB）；b64 解码错误包装；404 在前 3 次查询容忍（任务创建后查询端短暂延迟）；`extract_result_url` 公开化 + metadata 非 dict 防御；最后尝试不再白等 sleep
    - 测试扩至 71 项：_request 429 重试、create_video 不重试、轮询状态序列、429 外层退避（6s/12s 档）、非 JSON 容错、resolve_api_key 环境变量三分支、空数组省略、未知模式报错、空 batch、重定向拒内网、IPv4-mapped 拒绝、进度绝对值语义
    - 文档漂移清理：8 口表述、退避序列 3→6→12→24→30、api_key 随工作流明文保存的泄漏提示
  - v0.2.5（用户反馈：音频上传不方便）：
    - 全能参考模式新增 `audio_file` 本地上传口（`IO.Combo.Input(upload=IO.UploadType.audio)`，官方 LoadAudio 同款交互）：节点上出现"上传文件"按钮（点击弹文件选择框），新版前端亦支持把音频直接拖到节点；上传后文件存入 ComfyUI input 目录
    - `audio_file_to_data_uri`：本地音频按扩展名映射 mime（mp3/wav/m4a/aac/ogg/flac/opus/webm/wma）转 Data URI 随 audios 数组直传；单条 >15MB 报错引导剪辑。依据：图片 Data URI 直传已被用户真机验证（429 发生在轮询阶段，说明带 Data URI 的创建请求被接受），音频按同机制走，URL 方式保留兜底
    - 本地 Data URI 与 audio_urls 公网 URL 可混用，合并计数 ≤3 条
  - v0.2.6（用户反馈仍有 Agnes 字样）：全部文案（tooltip/报错/docstring/README/开发文档）统一改为 Agnes；仅 API 协议值——模型 ID（agnes-image-*、agnes-video-*）与服务域名（agnes-ai.cn 等）——保持服务端原样小写，全局替换前已确认大写 Agnes 仅出现在文案层
  - v0.2.7（用户反馈：video_url 填 URL 不方便）：全能参考模式新增 `video_file` 本地上传口（`upload=IO.UploadType.video`，官方 LoadVideo 同款交互，点击选择/拖拽）；`video_file_to_data_uri` 按扩展名映射 mime（mp4/webm/mov/mkv/avi/m4v）转 Data URI 直传 videos[0].url；单文件 >50MB 报错（API 限制 50MB、2-12 秒）；flash 模式对本地上传与 URL 视频参考一并拒绝；video_url 保留兜底
- **待办（真机联调）**
  - 用真实 API Key 跑通：文生图 → 图生图 → 文生视频 → 图生视频/首尾帧 → 全能参考
  - 实测定论：视频 API 是否接受 Data URI 本地图直传
  - GitHub 发布时补架构图并在 README 引用（用户约定）

## 7. 已知限制

- **V3-only**：包用 `comfy_entrypoint` 注册（加载器对传统映射与 V3 二选一），老版本 ComfyUI 无法加载，README 已注明版本要求
- 图像 API 无 seed/steps 参数，节点不提供（与 API 行为一致）
- 参考音频/视频只能填可公开访问的 URL（Agnes 无上传接口，ComfyUI 本地文件无法直接给到服务端）
- V3 的 `async execute` 内同步阻塞调用通过 `asyncio.to_thread` 移入线程池，轮询期间仍占用一个工作线程
- 示例工作流 JSON 的 V3 序列化可能因 ComfyUI 版本有差异，加载报错时需手动搭建（README 已注明）
  - v0.2.8（用户澄清：此前 agens 更名系口误，原品牌 Agnes 无误）：品牌全面回归 Agnes——显示名 "Agnes 图片生成"/"Agnes 视频生成"、节点 ID/类名 AgnesImageGenerate/AgnesVideoGenerate、模块文件 agnes_*.py、包目录 ComfyUI-AgnesAI、pyproject comfyui-agnes-ai、日志前缀 [Agnes AI]；仅 API 协议值（模型 ID/域名，本为小写 agnes）不变。注意：节点 ID 变更后，画布上由 Agens 版本保存的旧工作流需删节点重加
  - v0.2.9（用户要求：参考音/视频超限自动压缩、尽量保留质量）：
    - 视频（≤50MB）：transcode_video 用 PyAV（ComfyUI 视频功能必备依赖，环境必有）重编码 H.264——CRF22 原分辨率（视觉接近无损）→ 逐级降 CRF/缩放（26→30→30×0.75→32×0.5→35×0.5），每档达标即返回；丢弃音轨（require_audio=False 用不到）把码率预算留给画面；输出 mp4/yuv420p，尺寸取偶
    - 音频（≤15MB）：transcode_audio 用 PyAV 转 AAC 渐进降码率 192k→128k→96k→64k（WAV 等无损大文件一次转码即达标），输出 audio-only MP4（mime audio/mp4）
    - 两者首档失败（环境缺编码器等）即报错引导手动压缩；日志打印压缩前后大小；测试新增 5 项（合计 84 项全过）
  - v0.3.0（用户反馈：上传区应可关闭、图片只能上传一张太少）：
    - 全部上传槽 options 首项加「（不使用）」并作为默认——修复「默认开启还无法关闭」；占位/空值在组装时忽略
    - 上传槽数量对齐官方上限：参考图 8 个上传槽（flash 合并校验降为 5）+ 连线口（batch）；音频 3 个上传槽 + audio_urls 合并 ≤3；视频 1 个上传槽与 video_url 二选一（仅 2.5）
    - 新增 image_file_to_data_uri：本地上传图片原样直传优先（无损），超限经 PIL 走与参考图一致的 JPEG 渐进压缩；JPEG 阶梯抽为 _JPEG_STEPS 共享
    - 修复：媒体槽重构时 reference_images 未从 media_sel 提取、audio 组装误用循环外残留变量（测试捕获）
    - 测试扩至 91 项全过
  - v0.3.1（用户真机反馈：首尾帧 last_frame 边长 <256 被 API 400 拒绝）：
    - 新增 `_fit_side_limits`：参考图（首帧/尾帧/全能参考图，含连线与上传槽全路径）边长 <256 自动放大、>5760 自动等比缩小（LANCZOS）；宽高比超过约 22.5:1 无法同时满足两边时明确报错引导裁剪
    - 编码链重构：encode_frame_auto 拆出 `_encode_image_auto`（PIL 入口），tensor_to_data_uris 与 image_file_to_data_uri 共用尺寸适配 + 体积压缩；上传文件原样直传仍优先（尺寸合规时）
    - 测试扩至 96 项全过（含小图放大/大图缩小/极端比例报错/上传槽适配）
    - 教训：API 文档"宽高 256–5760px"限制此前只在全能参考调研时记录、未落到首尾帧实现
  - v0.3.2（用户反馈：上传下拉框太多、参考图应做成 IMAGE 接口那种）：
    - 参考图片回归 IMAGE 连线口：`reference_images` + `reference_images_2` 两个口（各支持 batch 多帧，Image Batch 合并后一条线可带多张），合并计数 ≤8（flash ≤5）；删除 8 个图片上传下拉框
    - 音频上传槽精简为 1 个（audio_file）+ audio_urls 多行 URL，合并 ≤3；视频保持 video_file/video_url 二选一
    - 全能参考媒体输入从 15 个减至 6 个；上传槽交互保持「点击上传按钮打开文件夹选择（或拖入）+（不使用）关闭」
    - 测试更新：schema 断言、双连线口合并/flash 上限/仅口 2 生效等用例，95 项全过
  - v0.4.0（用户要求参考 Bigesila-B/comfyui_ai_prompt 的素材添加思路）：
    - 复刻其核心机制：隐藏 STRING widget 存素材清单 JSON + `WEB_DIRECTORY` 前端扩展渲染上传面板（"添加素材"按钮打开系统文件框并支持多选/拖入虚线框上传/缩略图预览/单条移除/清空）+ 文件经内置 `/upload/image` 端点进 input 目录 + 后端把文件名转 Data URI
    - 我们的适配：清单条目带类型（image/audio/video），面板按类型显示图标与计数、超上限（图8/音3/视频1）在面板侧即时提示；后端 `_parse_media_files` 解析并过滤占位/坏条目，与连线口、URL 合并计数
    - 全能参考子输入精简为：双 IMAGE 连线口 + audio_urls + video_url（media_files 由面板维护、前端隐藏）
    - 测试 92 项全过；web/agnes_media.js 经 node --check 语法验证
  - v0.4.1（用户真机日志反馈：pending 状态刷屏 23 行、长任务固定 3s 查询触发 429）：
    - 真机确认任务最终成功（429 退避生效）；API 实际返回文档未记载的 pending 等待状态
    - poll_video：pending 归入等待类状态（_QUIET_TASK_STATUSES）静默处理；未知状态只提示一次（回到已知集合后重置）；基础间隔随已等待时长放缓（30s 内 3s → 90s 内 5s → 之后 8s），429 退避在其上叠加（interval + backoff，上限 +30s）
    - 测试新增 pending 静默、未知状态单次提示用例，合计 97 项全过
  - v0.4.2（用户真机反馈：面板报 "Cannot read properties of undefined (reading 'options')"）：
    - 根因：新版 ComfyUI 前端 api.fetchApi 内部行为变化，上传请求在其 api 层抛错
    - 修复：web/agnes_media.js 改用原生 fetch + api.apiURL("/upload/image") 直连上传端点，绕开前端 api 层差异；端点本身无鉴权，FormData 结构不变
    - 上传交互不变（按钮打开文件夹/拖入/多选/预览/删除/清空）
  - v0.5.0（用户反馈：两个连线口太少、audio_urls 不要、要统一素材框 + 首帧预览）：
    - 删除 reference_images/reference_images_2 连线口与 audio_urls 输入；参考媒体（图/音/视）全部经素材面板上传，与 video_url（视频参考备选）并存
    - 面板预览增强：图片缩略图、视频用 `<video preload=metadata>` 显示首帧画面、音频显示图标
    - 上限：图 ≤8（flash ≤5）、音 ≤3、视频 ≤1，面板侧即时计数提示 + 后端兜底校验
    - 测试重构：临时素材文件统一在开头创建/末尾清理；92 项全过 + node --check JS 验证
  - v0.5.1（代码审查驱动：修 P1 拼写 bug + 清理孤立代码）
    - **P1 修复**：agnes_api.py 的「轮询超时」与「任务 failed」两条分支把 `AgnesApiError` 误写成 `AgensApiError`，运行时抛 `NameError`——用户看不到「轮询超时／视频生成失败」的中文提示，服务端返回的失败原因也被吞掉。此前测试恰好未覆盖这两条分支故长期未暴露；现补 2 项回归用例，并已验证其有效性（临时还原拼写 → 测试以 NameError 失败退出）
    - **P2 修复**：`make_progress` 的降级 `_Noop` 补 `update_absolute`，与 ComfyUI `ProgressBar` 接口对齐（此前脱离 ComfyUI 调用会 AttributeError）
    - **P3 清理**：删除 v0.5.0 移除上传下拉框后遗留的孤立代码——`agnes_video.py` 的 `_NOT_USED`／`_resolve_upload`／`_list_input_files` 及随之无用的 `os` import、`utils.py` 的 `png_bytes_to_data_uri`
    - 注释订正：音频「无法客户端压缩」（v0.2.9 已支持 AAC 压缩）、图像「前 3 张免计费」（官方口径：当前各分辨率档位与输入参考图均免费）
    - 测试 92 → 94 项全过
  - v0.5.2（由「素材面板适用于什么模式」的提问引出的边界修复）
    - 修复：flash 模型的「不支持视频参考」校验原先位于模式判断之前（agnes_video.py），导致「文生视频」「图生视频 / 首尾帧」模式下只要素材面板残留视频就误报——而这两种模式根本不使用视频参考；现移入 reference 分支，仅「全能参考」模式校验。720P 降级校验保留在模式判断之前（分辨率对所有模式生效）
    - 补 1 项回归用例：flash + 文生视频 + 面板残留视频 → 正常生成且不发送 videos 字段
    - 测试 94 → 95 项全过
  - v0.5.3（用户提问「文生视频 / 首尾帧模式下为什么还有素材上传框」）：
    - 现象：素材面板是节点级组件，与 DynamicCombo 无关——`media_files` 是顶层输入，JS 侧完全没读 `media_mode`。结果「文生视频」「图生视频 / 首尾帧」下面板照常可用却完全不参与生成，属静默失效，极易被误判成上传故障
    - 交互决策（用户选定）：非「全能参考」模式**保留面板但置灰禁用**——加素材 / 清空按钮与文件选择器禁用、素材列表不可点击、拖入被忽略、整体降为 55% 不透明度，状态行提示「素材仅『全能参考』模式使用，当前模式已停用」
    - 实现：`web/agnes_media.js` 新增 `currentMode()`（兼容 `widget.value` 为选项文案或 `{media_mode: ...}` 对象两种形态）与 `applyModeState()`；钩子挂在节点级 `onWidgetChanged` 上而非单个 `widget.callback`（DynamicCombo 切换会重建输入口，后者会随之丢失）；`onConfigure` 复用同一入口，保证工作流加载后状态正确
    - 后端未改动：素材清单照常序列化保存，切回「全能参考」后原样恢复，不丢数据
    - 验证：`node --check`（按 ES module）语法通过；后端逻辑未动，Python 用例仍 95 项全过
  - v0.5.4（用户选定「其余优化项全做」，跳过 seed tooltip 文案）：
    - **图片自动落盘**（agnes_image.py）：`_save_png` 走官方 `folder_paths.get_save_image_path`（自带递增序号防覆盖）写入 `output/AgnesAI`，节点输出口由 1 个增为 `image` + `saved_path`（相对路径），并把落盘结果作为 `ui.images` 交给前端预览；写盘失败只打印告警、`saved_path` 返回空串、不构造 ui，保证「图已生成且可能已计费」时结果不丢
    - **排队阶段进度估算**（agnes_api.py `poll_video`）：服务端在 queued/pending 阶段不返回 `progress`，进度条长时间停在 0。现按已等待时长估算 `min(30, elapsed/timeout*30)` 上报；新增 `reported` 记录已上报值，只上报递增数——服务端真实 progress 起点低于估算值时不会回退（看着像卡住）
    - **素材面板上传前即时提示**（web/agnes_media.js）：不支持的文件不再静默丢弃，状态行列出被跳过的文件名；超出数量上限的文件被跳过并点名（可容纳的先上传，不再整批拒绝）；超过 API 体积上限（图/音 15MB、视频 50MB）的文件在上传后提示「发送时将自动压缩」
    - **前端置灰钩子修正**：v0.5.3 记录的节点级 `onWidgetChanged` 在本前端版本不触发（导致三个模式下均为灰色不可用）。改为挂钩 `media_mode` widget 的 `callback`（DynamicCombo 切换只触发此回调，回调入参为 `{media_mode: ...}` 动态值对象、`this` 指向 widget，故用闭包变量 `node`），并保留 `onDrawForeground` 模式比对兜底 + `onConfigure` 加载后同步
    - **新增前端测试** `tests/panel_test.mjs`：改写 `web/agnes_media.js` 的 import 为 globalThis 替身后写入临时 .mjs 动态导入，用最小 DOM 替身驱动 `onNodeCreated`，21 项断言覆盖两条置灰路径（callback / onDrawForeground 兜底）、禁用态拖入被忽略、onConfigure 恢复、跳过/超限/体积提示、`onWidgetChanged` 传满 4 参（reading 'options' 回归）
    - **Python 测试补栏**：新增 14 项（合计 109 项全过）——`saved_path` 输出口 schema、落盘相对路径/文件内容/ui 条目/不覆盖、落盘失败三断言、`_save_png` 单元、排队估算递增、估算值区间、完成上报 100、真实 progress 低于估算不回退
    - 未做：seed tooltip 补「相同 seed 可提高复现性」（用户明确跳过）
