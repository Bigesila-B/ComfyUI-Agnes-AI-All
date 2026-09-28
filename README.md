# ComfyUI Agnes AI

接入 [Agnes AI](https://agnes-ai.cn/zh-Hans/docs/overview) 多模态生成 API 的 ComfyUI 自定义节点（节点与文档统一用 Agnes；仅 API 协议值——模型 ID、服务域名——保持服务端原样小写），共 **2 个节点**：

| 节点 | 能力 | 输出 |
|---|---|---|
| **Agnes 图片生成** | 文生图 / 图生图 / 多图合成（8 个参考图输入口） | IMAGE + `saved_path`（自动落盘路径） |
| **Agnes 视频生成** | 文生视频 / 图生视频·首尾帧 / 全能参考（顶部模式下拉切换，输入口联动） | VIDEO（可直连官方 SaveVideo） |

> 本包使用 ComfyUI **V3 节点机制**注册（`comfy_entrypoint`），需要较新的 ComfyUI（2025 下半年及之后、内置 `comfy_api` 支持）。老版本将无法加载本包。

底层模型：`agnes-image-2.5-flash`（及其它 Image 系列）、`agnes-video-2.5` / `agnes-video-2.5-flash`。API 细节见官方文档索引：<https://wiki.agnes-ai.cn/llms.txt>。

## 安装

1. 把本目录（整个 `ComfyUI-Agnes-AI-All` 文件夹）放入 `ComfyUI/custom_nodes/`，重启 ComfyUI；
   或用 ComfyUI Manager 的 "Install via Git URL" 安装。
2. 依赖只有 `requests` 和 `pillow`（ComfyUI 环境通常已自带）。

## 配置 API Key

1. 注册并登录 Agnes AI 平台，在**开发者控制台**生成 API Key；
2. 在节点的 `api_key` 输入框填入；或设置环境变量 `AGNES_API_KEY`（节点留空时自动读取）。

> ⚠️ **安全提示**：填入节点的 `api_key` 会随工作流 JSON 明文保存，**分享工作流前务必清空该字段**。推荐优先使用环境变量 `AGNES_API_KEY`，节点留空时自动读取。

节点默认走国内站 `https://api.agnes-ai.cn/v1`；国际站用户把 `base_url` 改为 `https://apihub.agnes-ai.com/v1`。

## Agnes 图片生成

- **prompt**：多行文本框，与官方 CLIP Text Encode 的 text 一样可以**右键 → Convert to input**，接入任意 STRING 输出节点。
- **参考图 8 个输入口**：`image`、`image_2` … `image_8`（可选），与 API 8 张上限对齐。每个口都可以连一张图，也支持 batch 多帧；所有帧合并计算，1 张 = 图生图，多张 = 多图合成（最多 8 张）。
  - ComfyUI 一个输入口只能连一条线，要连更多图就把不同图分别接到 `image_2`~`image_8`，或用官方 `Image Batch` 节点把多图合并后接入同一个口。
- **model / size / ratio**：模型、分辨率档位（1K–4K）、宽高比（8 种）。
- 输出直接返回 IMAGE 张量，可接 SaveImage / Preview Image 等任何图像节点；同时**自动落盘**到 `ComfyUI/output/AgnesAI/`，并把相对路径从 `saved_path` 输出口给出（不想手动保存的可以不用再接 SaveImage）。落盘失败只会在控制台告警，不影响本次出图。

> 提示：图像 API 没有种子（seed）参数，重跑同一提示词每次结果都可能不同。

## Agnes 视频生成

**节点顶部的「模式」下拉即切换开关，切换后媒体输入口随之变化**：

| 模式 | 显示的输入口 | 说明 |
|---|---|---|
| 文生视频 | （无媒体口） | 纯文本描述生成视频 |
| 图生视频 / 首尾帧 | `first_frame`、`last_frame` | 只连首帧 = 图生视频；首尾帧同连 = 两帧过渡 |
| 全能参考 | **统一素材上传面板**（点击/拖入，图片/音频/视频混传，带预览）+ `video_url` 备选 | prompt 中用 `<Picture N>`、`<Audio N>`、`<Video N>` 占位说明各参考媒体的作用 |

- `first_frame` / `last_frame` / `reference_images` 接 ComfyUI 的 IMAGE（本地图片超限时自动压缩，再编码为 Data URI 上传）；
- **素材上传面板**：点击「＋ 添加素材」打开文件夹选择文件（支持多选），或把图片/音频/视频**直接拖到虚线框内**；素材以缩略图/名称列出（图片显示缩略图、**视频显示首帧画面**、音频显示图标），可单个移除或一键清空。文件通过 ComfyUI 上传到 input 目录，发送时自动转 Data URI 并做尺寸/体积适配；
- **图片可连续添加多张**（≤8 张，flash ≤5）、**音频 ≤3 条**（单条 ≤15MB 超限自动压缩）、**视频 ≤1 个**（2–12 秒、≤50MB 超限自动压缩，仅 `agnes-video-2.5` 支持）；超出数量上限的文件会被跳过并在状态行点名，可容纳的照常上传；
- **上传前即时提示**：不支持的文件（非图片/音频/视频）会被跳过并列出文件名；超过 API 体积上限（图/音 15MB、视频 50MB）的文件会提示「发送时将自动压缩」；
- **其他模式下素材面板置灰**：文生视频 / 图生视频·首尾帧不使用参考素材，切到这两种模式时面板整体变灰、按钮与拖入均不可用（状态行说明原因），切回「全能参考」自动恢复，已添加的素材不会丢失；
- `video_url` 备选：可公开访问的视频 URL（与面板上传的视频二选一）；
- **尺寸自动适配**：参考图边长小于 256px 自动放大、超过 5760px 自动等比缩小（API 要求 256–5760px），控制台提示调整动作；
- **超限自动压缩**：参考图片（≤15MB）、音频（≤15MB）、视频（≤50MB）超出限制时自动压缩，优先保留原有画质/音质（视频 H.264 CRF22 原分辨率起步、音频 AAC 192k 起步，逐级降档直到达标），控制台会打印压缩前后大小；
- `seconds` 4–12 秒；`size` 720P/1080P/1K/2K（`agnes-video-2.5-flash` 仅 720P，其他档位自动降级）；
- `seed` 0 表示随机；
- 输出为 ComfyUI 原生 **VIDEO** 资产，可直接接官方 **Save Video**，或用 **Get Video Components** 拆成帧/音频。

## 定价提醒（以官方定价页为准）

- 图像：当前各档位**免费**；
- 视频 2.5：720P ¥0.15/秒、1080P/1K ¥0.25/秒、2K ¥0.35/秒；**2.5 Flash 当前限免**。

## 目录结构

```
ComfyUI-Agnes-AI-All/
├── __init__.py            # V3 注册（comfy_entrypoint）
├── pyproject.toml         # Comfy Manager 安装清单
├── LICENSE                # MIT 许可证
├── icon.png               # Registry 图标（400×400）
├── nodes/
│   ├── agnes_api.py       # API 客户端（图像 / 视频任务创建与自适应退避轮询）
│   ├── agnes_image.py     # 图片生成节点（V3，8 个参考图口）
│   ├── agnes_video.py     # 视频生成节点（V3，DynamicCombo 模式切换）
│   └── utils.py           # 图像编解码 / URL 安全校验 / 下载 / VIDEO 类型兼容
├── tests/
│   ├── dryrun_test.py     # 无网络干跑测试（Python）
│   └── panel_test.mjs     # 素材面板前端逻辑测试（Node）
├── example/               # 示例工作流
└── docs/DEVELOPMENT.md    # 开发文档
```

测试命令（均无需 ComfyUI 环境）：

```bash
python tests/dryrun_test.py     # 109 项：schema / payload / 压缩 / 安全 / 落盘 / 轮询进度
node   tests/panel_test.mjs     # 21 项：素材面板置灰两条路径 + 上传前提示
```

## 常见问题

- **提示 "未配置 Agnes AI API Key"**：填 `api_key` 或设置环境变量 `AGNES_API_KEY` 后重启。
- **参考图太大**：节点内置自动压缩（优先无损，其次 JPEG 95 质量原图分辨率，最后逐级降采样），尽量保留画质；控制台会打印压缩前后大小。只有压到最小档仍超限才要求手动缩小。
- **查询限频（429）**：Agnes 查询接口限频较严，节点轮询已内置自适应退避（间隔从 3s 逐步拉长到最长 30s），遇到限频会在控制台提示并继续等待，无需干预。
- **视频任务超时**：高峰期排队久，调大 `poll_timeout`（默认 20 分钟，在节点的高级选项里）。
- **401 报错**：API Key 不对或未激活。
- **示例工作流加载异常**：V3 节点的工作流序列化因 ComfyUI 版本可能有差异，`example/` 里的 JSON 若加载报错，请手动添加节点并按上文连线。
- **要求 ComfyUI 版本**：2025 下半年及之后（支持 V3 `comfy_entrypoint` 注册与 `comfy_api` VIDEO 类型）。
