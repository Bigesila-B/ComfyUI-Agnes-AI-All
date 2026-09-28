"""Agnes 图片生成节点：文生图 / 图生图 / 多图合成（ComfyUI V3 节点）。

- prompt 可右键转换为输入口，由任意 STRING 输出节点接入。
- 参考图提供 8 个独立输入口（image ~ image_8），每个口都支持 batch 多帧；
  所有帧合并后每帧算一张参考图（1 张=图生图，多张=多图合成，最多 8 张），
  按口顺序取满配额即停，超出部分不编码。
- 底层为 Agnes 图像 API（模型 ID 保留官方拼写）；该 API 无 seed 参数；
  response_format 必须放 extra_body（官方文档的常见坑）。
- 生成结果除 IMAGE 输出外，还会自动落盘到 output/AgnesAI 并输出相对路径，
  免去必须再接一个 SaveImage 才能留存的麻烦（落盘失败不影响出图）。
"""

import asyncio
from pathlib import Path

from comfy_api.latest import IO

import folder_paths

from .agnes_api import BASE_URL_CN, IMAGE_MODELS, AgnesClient
from .utils import png_bytes_to_tensor, resolve_api_key, tensor_to_data_uris

MAX_REFERENCE_IMAGES = 8  # Agnes 图像 API 多图合成参考图上限（当前各分辨率档位与参考图均免费）

# 自动落盘的子目录与文件名前缀（走 ComfyUI 官方 get_save_image_path，自带递增序号防覆盖）
_SAVE_PREFIX = "AgnesAI/AgnesAI"


def _save_png(png: bytes) -> tuple[str, dict | None]:
    """把生成结果落盘到 output/AgnesAI，返回 (相对路径, ui 图片条目)。

    落盘失败只打印提示并返回空值：图已生成（且可能已计费），不应因写盘失败而丢掉结果。
    """
    try:
        folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            _SAVE_PREFIX, folder_paths.get_output_directory()
        )
        name = f"{filename}_{counter:05}_.png"
        Path(folder, name).write_bytes(png)
    except OSError as e:
        print(f"[Agnes AI] 图片已生成，但自动落盘失败（不影响本次出图）：{e}")
        return "", None
    rel = f"{subfolder}/{name}" if subfolder else name
    return rel, {"filename": name, "subfolder": subfolder, "type": "output"}


def _generate_sync(prompt, model, size, ratio, api_key, base_url, image_batches):
    """同步执行体（在线程池中运行，避免阻塞 ComfyUI 事件循环）。"""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt 为空：请输入生成指令，或从外部节点接入提示词。")
    key = resolve_api_key(api_key)

    uris = []
    remaining = MAX_REFERENCE_IMAGES
    for batch in image_batches:
        if remaining <= 0:
            break  # 配额已满，跳过后续口，避免为将被丢弃的帧做无谓编码
        uris.extend(tensor_to_data_uris(batch, max_images=remaining, label="参考图"))
        remaining = MAX_REFERENCE_IMAGES - len(uris)

    mode_desc = "多图合成" if len(uris) > 1 else ("图生图" if uris else "文生图")
    print(f"[Agnes AI] 图片生成：{model} / {size} / {ratio} / 模式={mode_desc} / 参考图={len(uris)}")

    client = AgnesClient(key, base_url)
    png = client.generate_image(model=model, prompt=prompt, size=size, ratio=ratio, image_uris=uris)
    rel_path, ui_image = _save_png(png)
    if rel_path:
        print(f"[Agnes AI] 已自动保存：{rel_path}")
    return png_bytes_to_tensor(png), rel_path, ui_image


class AgnesImageGenerate(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="AgnesImageGenerate",
            display_name="Agnes 图片生成",
            category="AgnesAI",
            description="文生图 / 图生图 / 多图合成",
            inputs=[
                IO.String.Input(
                    "prompt",
                    multiline=True,
                    tooltip="生成/编辑指令。可右键转换为输入口，接入任意 STRING 节点。",
                ),
                IO.Combo.Input("model", options=IMAGE_MODELS, default=IMAGE_MODELS[0]),
                IO.Combo.Input(
                    "size",
                    options=["1K", "2K", "3K", "4K"],
                    default="1K",
                    tooltip="输出分辨率档位（与 ratio 组合决定最终像素）",
                ),
                IO.Combo.Input(
                    "ratio",
                    options=["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"],
                    default="1:1",
                ),
                IO.String.Input(
                    "api_key",
                    default="",
                    optional=True,
                    tooltip="Agnes AI API Key；留空则读取环境变量 AGNES_API_KEY",
                ),
                IO.String.Input(
                    "base_url",
                    default=BASE_URL_CN,
                    optional=True,
                    tooltip="国内站默认；国际站为 https://apihub.agnes-ai.com/v1",
                ),
                IO.Image.Input(
                    "image",
                    optional=True,
                    tooltip="参考图 1（连上即图生图）。多口连接或单口 batch 多帧 = 多图合成（共 8 口对齐 API 上限）",
                ),
                IO.Image.Input("image_2", optional=True, tooltip="参考图 2"),
                IO.Image.Input("image_3", optional=True, tooltip="参考图 3"),
                IO.Image.Input("image_4", optional=True, tooltip="参考图 4"),
                IO.Image.Input("image_5", optional=True, tooltip="参考图 5"),
                IO.Image.Input("image_6", optional=True, tooltip="参考图 6"),
                IO.Image.Input("image_7", optional=True, tooltip="参考图 7"),
                IO.Image.Input("image_8", optional=True, tooltip="参考图 8"),
            ],
            outputs=[
                IO.Image.Output(display_name="image"),
                IO.String.Output(
                    display_name="saved_path",
                    tooltip="自动保存的相对路径（output/AgnesAI 下）；落盘失败时为空串",
                ),
            ],
        )

    @classmethod
    async def execute(cls, prompt, model, size, ratio, api_key=None, base_url=None,
                      image=None, image_2=None, image_3=None, image_4=None,
                      image_5=None, image_6=None, image_7=None, image_8=None) -> IO.NodeOutput:
        tensor, rel_path, ui_image = await asyncio.to_thread(
            _generate_sync,
            prompt, model, size, ratio, api_key, base_url,
            [b for b in (image, image_2, image_3, image_4, image_5, image_6, image_7, image_8)
             if b is not None],
        )
        ui = {"images": [ui_image]} if ui_image else None
        return IO.NodeOutput(tensor, rel_path, ui=ui)
