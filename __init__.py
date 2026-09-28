"""ComfyUI Agnes AI 节点包（V3 注册 + web 前端扩展）

两个节点：
- Agnes 图片生成：文生图 / 图生图 / 多图合成（8 个参考图输入口）
- Agnes 视频生成：文生视频 / 图生视频·首尾帧 / 全能参考（DynamicCombo 模式切换）
  全能参考模式带素材上传面板（web/agnes_media.js）：点击选择或拖入图片/音频/视频

注意：本包使用 V3 注册（comfy_entrypoint）。ComfyUI 加载器在有 NODE_CLASS_MAPPINGS
时会忽略 comfy_entrypoint，因此本包不能提供传统映射，需要较新的 ComfyUI 版本。

底层服务为 Agnes AI（模型 ID 保留官方拼写）。
文档：https://agnes-ai.cn/zh-Hans/docs/overview
"""

from typing_extensions import override

from comfy_api.latest import ComfyExtension

from .nodes.agnes_image import AgnesImageGenerate
from .nodes.agnes_video import AgnesVideoGenerate

WEB_DIRECTORY = "./web"

__all__ = ["WEB_DIRECTORY", "comfy_entrypoint"]


class AgnesExtension(ComfyExtension):
    @override
    async def get_node_list(self):
        return [AgnesImageGenerate, AgnesVideoGenerate]


async def comfy_entrypoint() -> AgnesExtension:
    return AgnesExtension()
