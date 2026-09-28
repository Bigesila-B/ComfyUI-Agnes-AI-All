"""Agnes 视频生成节点：文生视频 / 图生视频·首尾帧 / 全能参考（ComfyUI V3 节点）。

- 顶部下拉切换模式（DynamicCombo），媒体输入口随模式变化：
  · 文生视频            → 无媒体输入
  · 图生视频 / 首尾帧   → first_frame（只连此口即图生视频）+ last_frame（同连即首尾帧）
  · 全能参考            → 参考图连线口 + 8 个图片上传槽 + 3 个音频上传槽 + audio_urls
                          + 视频上传槽 + video_url（数量对齐 Agnes API 上限：
                          图 ≤8（flash ≤5）、音频 ≤3、视频 ≤1（仅 agnes-video-2.5））
- 每个上传槽都可选「（不使用）」关闭；点击上传按钮或把文件拖到节点上即可上传，
  文件存入 ComfyUI input 目录，超限时自动压缩（尽量保留画质/音质）。
- 底层 Agnes Video 2.5 / 2.5 Flash（V2.0 已下线；seconds 取代 num_frames/frame_rate）。
  模型 ID 为 API 协议值，保持服务端原样小写；节点对外名称统一用 Agnes。
"""

import asyncio
import json

from comfy_api.latest import IO

import folder_paths

from .agnes_api import BASE_URL_CN, VIDEO_MODELS, AgnesApiError, AgnesClient
from .utils import (
    assert_public_http_url,
    audio_file_to_data_uri,
    download_video_to_temp,
    image_file_to_data_uri,
    make_progress,
    resolve_api_key,
    tensor_to_data_uris,
    video_file_to_data_uri,
    video_from_file,
)

MAX_REFERENCE_IMAGES = 8     # agnes-video-2.5 参考图上限（2.5-flash 为 5）
FLASH_REFERENCE_IMAGES = 5
MAX_AUDIO_SOURCES = 3        # 参考音频上限（上传槽 + URL 合并计数）

MODE_TEXT = "text"
MODE_KEYFRAME = "keyframe"
MODE_REFERENCE = "reference"

# DynamicCombo 三个选项的显示文案 → API mode 值
MODE_LABELS = {
    "文生视频": MODE_TEXT,
    "图生视频 / 首尾帧": MODE_KEYFRAME,
    "全能参考": MODE_REFERENCE,
}

_VALID_MEDIA_TYPES = ("image", "audio", "video")


def _parse_media_files(value) -> list[dict]:
    """解析前端素材面板写入的 JSON 清单：[{"type": "image|audio|video", "name": "文件名 [input]"}]。"""
    try:
        entries = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        print("[Agnes AI] 素材清单格式无效，已忽略（media_files 不是合法 JSON 数组）")
        return []
    if not isinstance(entries, list):
        return []
    return [
        {"type": e["type"], "name": str(e["name"])}
        for e in entries
        if isinstance(e, dict) and e.get("type") in _VALID_MEDIA_TYPES
        and e.get("name") and not str(e["name"]).startswith("（")
    ]


def _run_video_sync(prompt, model, size, aspect_ratio, seconds, seed, api_key, base_url,
                    mode, media_sel: dict, video_url: str,
                    media_files_value: str, poll_timeout):
    """同步执行体（在线程池中运行，避免阻塞 ComfyUI 事件循环）。"""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt 为空：请输入视频描述，或从外部节点接入提示词。")
    key = resolve_api_key(api_key)

    first_frame = last_frame = None
    if mode == MODE_KEYFRAME:
        first_frame = media_sel.get("first_frame")
        last_frame = media_sel.get("last_frame")
        if first_frame is None and last_frame is None:
            raise ValueError("图生视频 / 首尾帧模式需要至少连接一张 first_frame（或 last_frame）图片。")
        for name, tensor in (("first_frame", first_frame), ("last_frame", last_frame)):
            if tensor is not None and tensor.shape[0] == 0:
                raise ValueError(f"{name} 收到空 batch（0 帧），请检查上游节点输出。")

    media_entries = _parse_media_files(media_files_value)
    upload_images = [e["name"] for e in media_entries if e["type"] == "image"]
    upload_audios = [e["name"] for e in media_entries if e["type"] == "audio"]
    upload_videos = [e["name"] for e in media_entries if e["type"] == "video"]
    video_url = (video_url or "").strip()

    # —— 2.5 Flash 限制：仅 720P、参考图上限 5（视频参考限制只在 reference 分支内校验）
    if model.endswith("flash"):
        if size != "720P":
            print(f"[Agnes AI] {model} 仅支持 720P，size 已从 {size} 降级为 720P。")
            size = "720P"

    payload = {"model": model, "prompt": prompt, "mode": mode, "seconds": str(seconds), "size": size}

    if mode == MODE_KEYFRAME:
        if first_frame is not None:
            payload["first_frame"] = tensor_to_data_uris(first_frame, label="首帧")[0]
        if last_frame is not None:
            payload["last_frame"] = tensor_to_data_uris(last_frame, label="尾帧")[0]
    elif mode == MODE_REFERENCE:
        limit = FLASH_REFERENCE_IMAGES if model.endswith("flash") else MAX_REFERENCE_IMAGES

        # 图片参考：全部来自素材面板（≤8 张，flash ≤5），尺寸/体积自动适配
        if len(upload_images) > limit:
            raise ValueError(
                f"{model} 参考图上限 {limit} 张（当前素材 {len(upload_images)} 张），请在面板中删减。"
            )
        image_uris = [
            image_file_to_data_uri(folder_paths.get_annotated_filepath(name), label=f"上传参考图 {i + 1}")
            for i, name in enumerate(upload_images)
        ]
        if image_uris:
            payload["images"] = image_uris

        # 音频参考：素材面板上传（≤3 条，单条 ≤15MB 超限自动压缩）
        audio_items = [
            audio_file_to_data_uri(folder_paths.get_annotated_filepath(name), label=f"上传音频 {i + 1}")
            for i, name in enumerate(upload_audios)
        ]
        if len(audio_items) > MAX_AUDIO_SOURCES:
            raise ValueError(
                f"参考音频最多 {MAX_AUDIO_SOURCES} 条（当前 {len(audio_items)} 条），请在面板中删减。"
            )
        if audio_items:
            payload["audios"] = audio_items

        # 视频参考：素材面板 1 个 或 video_url（二选一，同时提供报错；仅 agnes-video-2.5）
        if model.endswith("flash") and (upload_videos or video_url):
            raise ValueError(f"{model} 不支持视频参考，请改用 agnes-video-2.5 或移除视频参考输入。")
        if len(upload_videos) > 1:
            raise ValueError("视频参考最多 1 个，请只保留一个上传视频。")
        if upload_videos and video_url:
            raise ValueError("视频参考只能提供一个：请在上传视频与 video_url 中二选一。")
        video_item = None
        if upload_videos:
            video_item = {
                "url": video_file_to_data_uri(
                    folder_paths.get_annotated_filepath(upload_videos[0]), label=f"上传视频 {upload_videos[0]}"),
                "start_seconds": 0,
                "require_audio": False,
            }
        elif video_url:
            video_item = {
                "url": assert_public_http_url(video_url, field="参考视频 URL"),
                "start_seconds": 0,
                "require_audio": False,
            }
        if video_item:
            payload["videos"] = [video_item]

        if not payload.get("images") and not payload.get("audios") and not payload.get("videos"):
            raise ValueError("全能参考模式至少需要一类参考媒体（图/音频/视频）。")

    if aspect_ratio:
        payload["aspect_ratio"] = aspect_ratio
    if seed and int(seed) > 0:
        payload["seed"] = int(seed)

    print(f"[Agnes AI] 视频生成：{model} / {size} / {aspect_ratio} / {seconds}s / 模式={mode}")

    client = AgnesClient(key, base_url)
    try:
        task = client.create_video(payload)
    except AgnesApiError as e:
        raise AgnesApiError(
            f"{e}\n（创建接口不自动重试，以防重复建任务、按秒重复计费；"
            "若不确定任务是否已创建，请到 Agnes 控制台核对）"
        ) from e
    task_id = task.get("video_id") or task.get("task_id") or task.get("id")
    print(f"[Agnes AI] 任务已创建：{task_id}，开始轮询…")

    # progress 为累计百分比（0-100），用绝对值语义更新 ComfyUI 进度条
    progress = make_progress(100)
    result = client.poll_video(task_id, model, timeout=float(poll_timeout),
                               progress_cb=progress.update_absolute)
    video_url_result = client.extract_result_url(result)
    print(f"[Agnes AI] 任务完成：{video_url_result}")

    local_path = download_video_to_temp(video_url_result, folder_paths.get_temp_directory())
    return video_from_file(local_path)


class AgnesVideoGenerate(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="AgnesVideoGenerate",
            display_name="Agnes 视频生成",
            category="AgnesAI",
            description="文生视频 / 图生视频·首尾帧 / 全能参考，切换模式后输入口随之变化",
            inputs=[
                IO.DynamicCombo.Input(
                    "media_mode",
                    options=[
                        IO.DynamicCombo.Option("文生视频", []),
                        IO.DynamicCombo.Option("图生视频 / 首尾帧", [
                            IO.Image.Input(
                                "first_frame",
                                optional=True,
                                tooltip="首帧图。只连此口 = 图生视频",
                            ),
                            IO.Image.Input(
                                "last_frame",
                                optional=True,
                                tooltip="尾帧图（与首帧同连 = 首尾帧过渡）",
                            ),
                        ]),
                        IO.DynamicCombo.Option("全能参考", [
                            IO.String.Input(
                                "video_url",
                                default="",
                                optional=True,
                                tooltip="参考视频（可公开访问 URL，仅 agnes-video-2.5 支持），与素材面板上传的视频二选一。参考图/音频请用节点上方的素材面板上传。",
                            ),
                        ]),
                    ],
                    optional=True,
                    tooltip="选择生成模式：切换后下方的媒体输入口会随之变化",
                ),
                IO.String.Input(
                    "prompt",
                    multiline=True,
                    tooltip="视频描述。全能参考模式中用 <Picture 1>/<Audio 1>/<Video 1> 占位说明各参考媒体的作用。可右键转换为输入口。",
                ),
                IO.Combo.Input("model", options=VIDEO_MODELS, default=VIDEO_MODELS[0]),
                IO.Combo.Input(
                    "seconds",
                    options=["4", "5", "6", "7", "8", "9", "10", "11", "12"],
                    default="5",
                    tooltip="视频时长（秒）。2.5 系列不再使用 num_frames/frame_rate。",
                ),
                IO.Combo.Input(
                    "size",
                    options=["720P", "1080P", "1K", "2K"],
                    default="720P",
                    tooltip="2.5-flash 仅支持 720P，其他档位会自动降级。",
                ),
                IO.Combo.Input(
                    "aspect_ratio",
                    options=["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
                    default="16:9",
                ),
                IO.Int.Input(
                    "seed",
                    default=0,
                    min=0,
                    max=2147483647,
                    control_after_generate=True,
                    optional=True,
                    tooltip="0 = 不指定（服务端随机）；>0 时随请求发送",
                ),
                IO.String.Input(
                    "api_key",
                    default="",
                    optional=True,
                    tooltip="Agnes AI API Key；留空则读取环境变量 AGNES_API_KEY。注意：填入的 key 会随工作流 JSON 保存，分享工作流前请清空。",
                ),
                IO.String.Input(
                    "base_url",
                    default=BASE_URL_CN,
                    optional=True,
                    tooltip="国内站默认（需以 /v1 结尾）；国际站为 https://apihub.agnes-ai.com/v1",
                ),
                IO.Int.Input(
                    "poll_timeout",
                    default=1200,
                    min=60,
                    max=7200,
                    optional=True,
                    advanced=True,
                    tooltip="轮询超时（秒）；高峰期任务排队久可调大",
                ),
                IO.String.Input(
                    "media_files",
                    default="[]",
                    optional=True,
                    tooltip="素材面板上传清单（JSON，由上方上传区维护，无需手动编辑）",
                ),
            ],
            outputs=[IO.Video.Output(display_name="video")],
        )

    @classmethod
    async def execute(cls, prompt, model, seconds, size, aspect_ratio, seed=None,
                      api_key=None, base_url=None, video_url=None,
                      poll_timeout=None, media_mode=None, media_files="[]") -> IO.NodeOutput:
        sel = media_mode or {}
        mode_name = sel.get("media_mode")
        if mode_name is None:
            mode = MODE_TEXT
        else:
            if mode_name not in MODE_LABELS:
                raise ValueError(
                    f"未知的生成模式 {mode_name!r}，合法值：{list(MODE_LABELS)}。"
                    "该工作流可能由其他版本的节点保存，请重新选择模式后再运行。"
                )
            mode = MODE_LABELS[mode_name]
        if mode == MODE_KEYFRAME:
            first_frame = sel.get("first_frame")
            last_frame = sel.get("last_frame")
            if first_frame is None and last_frame is None:
                raise ValueError("图生视频 / 首尾帧模式需要至少连接一张 first_frame（或 last_frame）图片。")
        else:
            first_frame = last_frame = None

        video = await asyncio.to_thread(
            _run_video_sync,
            prompt, model, size, aspect_ratio, seconds, seed, api_key, base_url,
            mode, sel, video_url or "", media_files or "[]", poll_timeout or 1200,
        )
        return IO.NodeOutput(video)
