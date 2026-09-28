"""无网络干跑测试：stub 掉 torch/folder_paths/comfy/comfy_api.latest/网络 依赖，
验证两个 V3 节点的 schema、payload 组装、模式切换、参考图多口、自动压缩与安全校验。

运行：python tests/dryrun_test.py（无需 ComfyUI 环境，需要 numpy/Pillow/requests）
"""

import asyncio
import base64
import io
import ipaddress
import json
import socket as _socket
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------- stub 依赖
torch_stub = types.ModuleType("torch")
torch_stub.uint8 = "uint8"
torch_stub.from_numpy = lambda *a, **k: object()
sys.modules["torch"] = torch_stub

fp_stub = types.ModuleType("folder_paths")
fp_stub.get_temp_directory = lambda: "."
fp_stub.get_input_directory = lambda: "."
# 图片节点自动落盘：真实实现走 output/<子目录>，这里落到测试目录便于断言与清理
_SAVE_ROOT = Path(__file__).resolve().parent / "_tmp_output"
fp_stub.get_output_directory = lambda: str(_SAVE_ROOT)


def _stub_get_save_image_path(prefix, output_dir, *args, **kwargs):
    """对齐 ComfyUI 的 (folder, filename, counter, subfolder, prefix) 返回结构。"""
    parent = Path(prefix).parent
    subfolder = "" if str(parent) in ("", ".") else str(parent)
    name = Path(prefix).name
    folder = Path(output_dir) / subfolder if subfolder else Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    counter = len(list(folder.glob(f"{name}_*.png"))) + 1
    return str(folder), name, counter, subfolder, prefix


fp_stub.get_save_image_path = _stub_get_save_image_path
fp_stub.filter_files_content_types = lambda names, types: [
    n for n in names if n.lower().endswith((".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus"))
]
fp_stub.get_annotated_filepath = lambda name: str(Path(__file__).resolve().parent / str(name).replace(" [input]", "").replace(" [output]", "").replace(" [temp]", ""))
sys.modules["folder_paths"] = fp_stub

comfy_stub = types.ModuleType("comfy")
comfy_utils_stub = types.ModuleType("comfy.utils")
BAR_CALLS = []


def _stub_progress(total):
    return types.SimpleNamespace(
        update=lambda n=1: None,
        update_absolute=lambda v: BAR_CALLS.append(v),
    )


comfy_utils_stub.ProgressBar = _stub_progress
sys.modules["comfy"] = comfy_stub
sys.modules["comfy.utils"] = comfy_utils_stub

te_stub = types.ModuleType("typing_extensions")
te_stub.override = lambda f: f
sys.modules["typing_extensions"] = te_stub


# ---- comfy_api.latest V3 stub：让 define_schema() 可调用、NodeOutput 可取值
class _InputSpec:
    def __init__(self, kind, name=None, **kw):
        self.kind = kind
        self.name = name
        self.kw = kw


class _InputNS:
    def __init__(self, kind):
        self._kind = kind

    def Input(self, name=None, **kw):
        return _InputSpec(self._kind, name, **kw)

    def Output(self, name=None, **kw):
        return _InputSpec(self._kind + ".Output", name, **kw)


class _DynamicComboNS:
    def Input(self, name=None, **kw):
        return _InputSpec("DynamicCombo", name, **kw)

    class Option:
        def __init__(self, label, inputs=None, **kw):
            self.label = label
            self.inputs = inputs or []


class _IO:
    ComfyNode = type("ComfyNode", (), {})

    class Schema:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class NodeOutput:
        def __init__(self, *outputs, ui=None):
            self.outputs = outputs
            self.ui = ui

        def __getitem__(self, i):
            return self.outputs[i]

    String = _InputNS("String")
    Combo = _InputNS("Combo")
    Int = _InputNS("Int")
    Image = _InputNS("Image")
    Video = _InputNS("Video")
    DynamicCombo = _DynamicComboNS()  # 需为实例，否则 Input 的首个位置参数会被当成 self
    UploadType = types.SimpleNamespace(image="image", video="video", audio="audio")


latest_stub = types.ModuleType("comfy_api.latest")
latest_stub.ComfyExtension = type("ComfyExtension", (), {})
latest_stub.io = _IO()
latest_stub.IO = _IO()
comfy_api_stub = types.ModuleType("comfy_api")
comfy_api_stub.latest = latest_stub
sys.modules["comfy_api"] = comfy_api_stub
sys.modules["comfy_api.latest"] = latest_stub

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import nodes.agnes_api as agnes_api  # noqa: E402
import nodes.utils as agnes_utils  # noqa: E402
import nodes.agnes_video as agnes_video_mod  # noqa: E402
from nodes.agnes_image import AgnesImageGenerate  # noqa: E402
from nodes.agnes_video import AgnesVideoGenerate  # noqa: E402

# ------------------------------------------------------------ 无 DNS 的地址解析 stub
_PUBLIC = "93.184.216.34"


def fake_getaddrinfo(host, port=None):
    ip = host if _is_ip(host) else _PUBLIC
    if host in ("127.0.0.1", "localhost"):
        ip = "127.0.0.1"
    return [(2, 1, 6, "", (ip, 0))]


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


agnes_utils.socket.getaddrinfo = fake_getaddrinfo

# ------------------------------------------------------------ 真实小 PNG（验证编解码链路）
_buf = io.BytesIO()
Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8)).save(_buf, format="PNG")
_REAL_PNG = _buf.getvalue()
_PNG_B64 = base64.b64encode(_REAL_PNG).decode()

# ------------------------------------------------------------ 网络层拦截
captured = {}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def fake_post(url, headers=None, json=None, params=None, timeout=None, **kw):
    captured["post_url"] = url
    captured["json"] = json
    if "/images/generations" in url:
        return FakeResponse({"created": 1, "data": [{"url": None, "b64_json": _PNG_B64}]})
    return FakeResponse({"id": "task_x", "video_id": "video_x", "status": "queued"})


def fake_get(url, headers=None, params=None, timeout=None, **kw):
    captured["get_url"] = url
    return FakeResponse({
        "id": "video_x", "video_id": "video_x", "status": "completed",
        "progress": 100, "url": "https://cdn.example.com/out.mp4",
    })


agnes_api.requests.request = lambda method, url, **kw: (
    fake_post(url, **kw) if method == "POST" else fake_get(url, **kw)
)


def fake_download_video(url, temp_dir, prefix="agnes_video", retries=3):
    captured["download_url"] = url
    return "C:/fake/fake.mp4"


agnes_video_mod.download_video_to_temp = fake_download_video
agnes_video_mod.video_from_file = lambda path: ("VIDEO_ASSET", path)

# 帧编码 stub：payload 测试统一用合规尺寸的小图 + 固定 PNG；真实管线测试在后文恢复原实现
_orig_frame_to_image = agnes_utils._frame_to_image
_orig_encode_image_auto = agnes_utils._encode_image_auto
agnes_utils._frame_to_image = lambda frame: Image.new("RGB", (256, 256))
agnes_utils._encode_image_auto = lambda img, label="参考图": (_REAL_PNG, "image/png")


def png_bytes_to_tensor_noop(png):
    return ("IMAGE_TENSOR",)


agnes_utils.png_bytes_to_tensor = png_bytes_to_tensor_noop
agnes_image_mod = sys.modules["nodes.agnes_image"]
agnes_image_mod.png_bytes_to_tensor = png_bytes_to_tensor_noop

FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class _StubBatch:
    def __init__(self, frames=1):
        self.shape = (frames, 8, 8, 3)

    def __getitem__(self, idx):
        return object()  # encode_frame_auto 已被 stub，帧内容不影响测试


class _RealFrame:
    """模拟 torch tensor 接口，让真实 encode_frame_auto 产出可编码的 PIL 图。"""

    def __init__(self, arr):
        self._arr = arr  # uint8 [H, W, C]

    def detach(self):
        return self

    def cpu(self):
        return self

    def clamp(self, a, b):
        return self

    def mul(self, n):
        return self

    def round(self):
        return self

    def to(self, dtype):
        return self

    def numpy(self):
        return self._arr


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ 0. 测试用临时素材文件
_TMP_DIR = Path(__file__).resolve().parent
_TEST_AUDIO = _TMP_DIR / "_tmp_test_audio.mp3"
_TEST_VIDEO = _TMP_DIR / "_tmp_test_video.mp4"
_TEST_AUDIO.write_bytes(b"ID3fake mp3 bytes for test")
_TEST_VIDEO.write_bytes(b"ftypfake mp4 bytes")
for _i in (1, 2):
    Image.new("RGB", (300, 200)).save(_TMP_DIR / f"_tmp_panel_{_i}.png", format="PNG")

# ------------------------------------------------------------ 1. Schema 冒烟
img_schema = AgnesImageGenerate.define_schema()
vid_schema = AgnesVideoGenerate.define_schema()
img_inputs = {s.name for s in img_schema.inputs}
vid_inputs = {s.name for s in vid_schema.inputs}
check("图像节点 schema 完整（含 8 个参考图口）",
      {"prompt", "model", "size", "ratio", "api_key", "base_url",
       "image", "image_2", "image_3", "image_4", "image_5", "image_6", "image_7", "image_8"} <= img_inputs,
      img_inputs)
check("图像节点显示名", img_schema.display_name == "Agnes 图片生成", img_schema.display_name)
check("图像节点输出为 image + saved_path",
      [s.kw.get("display_name") for s in img_schema.outputs] == ["image", "saved_path"],
      [s.kw.get("display_name") for s in img_schema.outputs])
check("视频节点 schema 完整（含素材清单 media_files）",
      {"prompt", "model", "seconds", "size", "aspect_ratio", "seed", "api_key", "base_url", "poll_timeout", "media_mode", "media_files"} <= vid_inputs,
      vid_inputs)
check("视频节点显示名", vid_schema.display_name == "Agnes 视频生成", vid_schema.display_name)
dc = next(s for s in vid_schema.inputs if s.kind == "DynamicCombo")
check("DynamicCombo 三模式选项", [o.label for o in dc.kw["options"]] == ["文生视频", "图生视频 / 首尾帧", "全能参考"])
ref_opt = next(o for o in dc.kw["options"] if o.label == "全能参考")
check("全能参考子输入仅 video_url（素材全走上传面板）",
      [i.name for i in ref_opt.inputs] == ["video_url"],
      [i.name for i in ref_opt.inputs])

# ------------------------------------------------------------ 2. 文生视频 payload
captured.clear()
run(AgnesVideoGenerate().execute(
    prompt="夜空下的城市延时摄影", model="agnes-video-2.5", seconds="5", size="720P",
    aspect_ratio="16:9", seed=0, api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
    media_mode={"media_mode": "文生视频"},
))
body = captured["json"]
check("文生视频 mode=text", body.get("mode") == "text", body)
check("seconds 为字符串", body.get("seconds") == "5", body.get("seconds"))
check("size/aspect_ratio 正确", body.get("size") == "720P" and body.get("aspect_ratio") == "16:9")
check("seed=0 不发送", "seed" not in body, body)
check("创建任务 URL 为 /v1/videos", captured["post_url"].endswith("/v1/videos"), captured["post_url"])
check("轮询 URL 为域名根 /agnesapi", captured["get_url"].endswith("/agnesapi"), captured["get_url"])
check("完成视频走顶层 url 下载", captured.get("download_url") == "https://cdn.example.com/out.mp4")

# media_mode 为 None（未切换）时默认文生视频
captured.clear()
run(AgnesVideoGenerate().execute(
    prompt="默认模式", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
    api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
))
check("media_mode 未提供默认 text", captured["json"].get("mode") == "text", captured["json"])

# ------------------------------------------------------------ 3. 图生视频 payload（仅首帧）
captured.clear()
run(AgnesVideoGenerate().execute(
    prompt="女孩缓缓回头", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="9:16",
    seed=42, api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
    media_mode={"media_mode": "图生视频 / 首尾帧", "first_frame": _StubBatch(1)},
))
body = captured["json"]
check("图生视频 mode=keyframe", body.get("mode") == "keyframe", body)
check("仅传 first_frame", "first_frame" in body and "last_frame" not in body, list(body.keys()))
check("first_frame 为 Data URI", str(body.get("first_frame", "")).startswith("data:image/png;base64,"))
check("seed>0 随请求发送", body.get("seed") == 42, body.get("seed"))

# ------------------------------------------------------------ 4. 首尾帧 payload + flash 降级
captured.clear()
run(AgnesVideoGenerate().execute(
    prompt="从白天过渡到黑夜", model="agnes-video-2.5-flash", seconds="8", size="1080P", aspect_ratio="1:1",
    api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
    media_mode={"media_mode": "图生视频 / 首尾帧", "first_frame": _StubBatch(1), "last_frame": _StubBatch(1)},
))
body = captured["json"]
check("首尾帧同时传 first+last", "first_frame" in body and "last_frame" in body, list(body.keys()))
check("flash 非 720P 自动降级", body.get("size") == "720P", body.get("size"))

# keyframe 模式一张图都不连 → 报错
try:
    run(AgnesVideoGenerate().execute(
        prompt="x", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
        api_key="sk", base_url=agnes_api.BASE_URL_CN,
        media_mode={"media_mode": "图生视频 / 首尾帧"},
    ))
    check("keyframe 无帧图报错", False, "未抛错")
except ValueError as e:
    check("keyframe 无帧图报错", "first_frame" in str(e))

# ------------------------------------------------------------ 5. 全能参考 payload（素材面板图 + 音频）
captured.clear()
run(AgnesVideoGenerate().execute(
    prompt="以 <Picture 1> 的角色形象为核心，跟随 <Audio 1> 的节奏运动", model="agnes-video-2.5",
    seconds="10", size="1080P", aspect_ratio="16:9", api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
    media_mode={"media_mode": "全能参考"},
    media_files=(
        '[{"type": "image", "name": "_tmp_panel_1.png [input]"},'
        ' {"type": "image", "name": "_tmp_panel_2.png [input]"},'
        ' {"type": "audio", "name": "_tmp_test_audio.mp3 [input]"}]'
    ),
))
body = captured["json"]
check("全能参考 mode=reference", body.get("mode") == "reference", body)
check("images 为 2 张 Data URI", len(body.get("images", [])) == 2)
check("audios 解析 1 条面板音频", len(body.get("audios", [])) == 1)
check("不传 videos 字段当未填", "videos" not in body, list(body.keys()))

# ------------------------------------------------------------ 6. 参考视频 + flash 限制
captured.clear()
run(AgnesVideoGenerate().execute(
    prompt="参考 <Video 1> 的镜头节奏", model="agnes-video-2.5", seconds="5", size="720P",
    aspect_ratio="21:9", api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
    media_mode={"media_mode": "全能参考"},
    video_url="https://cdn.example.com/ref.mp4",
))
body = captured["json"]
check("video_url 组装 videos 数组",
      body.get("videos") == [{"url": "https://cdn.example.com/ref.mp4", "start_seconds": 0, "require_audio": False}],
      body.get("videos"))

try:
    run(AgnesVideoGenerate().execute(
        prompt="x", model="agnes-video-2.5-flash", seconds="5", size="720P", aspect_ratio="16:9",
        api_key="sk", base_url=agnes_api.BASE_URL_CN,
    media_mode={"media_mode": "全能参考"},
    video_url="https://cdn.example.com/ref.mp4",
    ))
    check("flash 拒绝视频参考", False, "未抛错")
except ValueError as e:
    check("flash 拒绝视频参考", "video_url" in str(e) or "视频参考" in str(e))

# ------------------------------------------------------------ 7. 图像节点 payload（文生图）
captured.clear()
out, saved_path = run(AgnesImageGenerate().execute(
    prompt="月光下的白猫", model="agnes-image-2.5-flash", size="2K", ratio="16:9",
    api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
)).outputs
body = captured["json"]
check("图像 URL 正确", captured["post_url"].endswith("/images/generations"), captured["post_url"])
check("response_format 在 extra_body 内", body.get("extra_body", {}).get("response_format") == "b64_json", body)
check("无参考图时 extra_body 不含 image", "image" not in body.get("extra_body", {}), body)
check("size/ratio 传档位", body.get("size") == "2K" and body.get("ratio") == "16:9", body)
check("b64_json 解码为 IMAGE 输出", out == ("IMAGE_TENSOR",), out)

# ------------------------------------------------------------ 8. 多图合成（8 个输入口）
captured.clear()
out, _ = run(AgnesImageGenerate().execute(
    prompt="把三个角色合成一张画", model="agnes-image-2.5-flash", size="1K", ratio="1:1",
    api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
    image=_StubBatch(1), image_2=_StubBatch(1), image_3=_StubBatch(2),
)).outputs
body = captured["json"]
check("image/image_2/image_3 多口共 4 张", len(body.get("extra_body", {}).get("image", [])) == 4,
      len(body.get("extra_body", {}).get("image", [])))

captured.clear()
out, _ = run(AgnesImageGenerate().execute(
    prompt="八图合成", model="agnes-image-2.5-flash", size="1K", ratio="1:1",
    api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
    image_5=_StubBatch(1), image_6=_StubBatch(1), image_7=_StubBatch(1), image_8=_StubBatch(1),
)).outputs
body = captured["json"]
check("image_5~image_8 后四口各 1 张", len(body.get("extra_body", {}).get("image", [])) == 4,
      len(body.get("extra_body", {}).get("image", [])))

captured.clear()
out, _ = run(AgnesImageGenerate().execute(
    prompt="单图编辑", model="agnes-image-2.5-flash", size="1K", ratio="1:1",
    api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
    image=_StubBatch(1),
)).outputs
body = captured["json"]
check("单口图生图 1 张", len(body.get("extra_body", {}).get("image", [])) == 1)

# ------------------------------------------------------------ 9. 参考图自动压缩管线（真实编码）
# 恢复真实 _frame_to_image / _encode_image_auto
agnes_utils._frame_to_image = _orig_frame_to_image
agnes_utils._encode_image_auto = _orig_encode_image_auto
_orig_encode_frame_auto = agnes_utils.encode_frame_auto

# 9.1 降采样单元
big = Image.new("RGB", (4096, 2048))
check("降采样 4096→1024 等比", agnes_utils._downscale(big, 1024).size == (1024, 512),
      agnes_utils._downscale(big, 1024).size)
nochange = agnes_utils._downscale(Image.new("RGB", (512, 512)), 2048)
check("小于目标长边不缩放", nochange.size == (512, 512), nochange.size)

# 9.2 正常小图走 PNG 无损
data, mime = agnes_utils.encode_frame_auto(_RealFrame(np.zeros((16, 16, 3), dtype=np.uint8)))
check("小图 PNG 直出无损", mime == "image/png" and len(data) <= agnes_utils.MAX_REF_IMAGE_BYTES,
      (mime, len(data)))

# 9.3 超限触发 JPEG 阶梯（临时调小上限模拟 33MB 大图场景）
_orig_max = agnes_utils.MAX_REF_IMAGE_BYTES
try:
    agnes_utils.MAX_REF_IMAGE_BYTES = 8000
    noise = np.random.randint(0, 255, (64, 64, 3), dtype=np.uint8)
    data, mime = agnes_utils.encode_frame_auto(_RealFrame(noise), label="压缩测试图")
    check("超限自动转 JPEG 且达标", mime == "image/jpeg" and len(data) <= 8000, (mime, len(data)))

    # 9.4 全档超限报错
    agnes_utils.MAX_REF_IMAGE_BYTES = 500
    try:
        agnes_utils.encode_frame_auto(_RealFrame(noise), label="压缩测试图")
        check("全档超限报错", False, "未抛错")
    except ValueError as e:
        check("全档超限报错", "15MB" in str(e))
finally:
    agnes_utils.MAX_REF_IMAGE_BYTES = _orig_max

# 9.5 tensor_to_data_uris 的 mime 随压缩结果变化（小图会被放大到 256，上限相应放宽）
agnes_utils.MAX_REF_IMAGE_BYTES = 40000
_noise = np.random.randint(0, 255, (64, 64, 3), dtype=np.uint8)


class _RealBatch:
    def __init__(self, arr, frames=1):
        self.shape = (frames, arr.shape[0], arr.shape[1], arr.shape[2])
        self._arr = arr

    def __getitem__(self, idx):
        return _RealFrame(self._arr)


uris = agnes_utils.tensor_to_data_uris(_RealBatch(_noise))
check("超限时 Data URI 为 jpeg", bool(uris) and uris[0].startswith("data:image/jpeg;base64,"),
      uris[0][:40] if uris else "无")
agnes_utils.MAX_REF_IMAGE_BYTES = _orig_max
uris2 = agnes_utils.tensor_to_data_uris(_RealBatch(np.zeros((16, 16, 3), dtype=np.uint8)))
check("正常路径 Data URI 为 png", bool(uris2) and uris2[0].startswith("data:image/png;base64,"),
      uris2[0][:40] if uris2 else "无")
# 压缩管线测试结束，恢复 stub 供后续 payload 测试使用
agnes_utils._frame_to_image = lambda frame: Image.new("RGB", (256, 256))
agnes_utils._encode_image_auto = lambda img, label="参考图": (_REAL_PNG, "image/png")

# ------------------------------------------------------------ 10. 空值与安全防御
try:
    run(AgnesImageGenerate().execute(
        prompt="  ", model="agnes-image-2.5-flash", size="1K", ratio="1:1",
        api_key="sk", base_url=agnes_api.BASE_URL_CN,
    ))
    check("空 prompt 报错", False, "未抛错")
except ValueError as e:
    check("空 prompt 报错", "prompt" in str(e))

try:
    run(AgnesImageGenerate().execute(
        prompt="cat", model="agnes-image-2.5-flash", size="1K", ratio="1:1",
        api_key="", base_url=agnes_api.BASE_URL_CN,
    ))
    check("缺 API Key 报错", False, "未抛错")
except ValueError as e:
    check("缺 API Key 报错", "API Key" in str(e))

try:
    run(AgnesVideoGenerate().execute(
        prompt="x", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
        api_key="sk", base_url="http://127.0.0.1/v1",
    ))
    check("私网 base_url 拒绝", False, "未抛错")
except ValueError as e:
    check("私网 base_url 拒绝", "非公网" in str(e))

try:
    run(AgnesVideoGenerate().execute(
        prompt="x", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
        api_key="sk", base_url="https://198.18.2.87/v1",
        media_mode={"media_mode": "文生视频"},
    ))
    check("fake-ip 段（198.18/15）base_url 放行", True)
except ValueError as e:
    check("fake-ip 段（198.18/15）base_url 放行", False, str(e))

try:
    run(AgnesVideoGenerate().execute(
        prompt="x", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
        api_key="sk", base_url="https://2001:2::248/v1",
        media_mode={"media_mode": "文生视频"},
    ))
    check("IPv6 fake-ip 段（2001:2::/48）base_url 放行", True)
except ValueError as e:
    check("IPv6 fake-ip 段（2001:2::/48）base_url 放行", False, str(e))

try:
    run(AgnesVideoGenerate().execute(
        prompt="x", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
        api_key="sk", base_url="https://192.168.1.1/v1",
    ))
    check("真内网 base_url 仍拒绝", False, "未抛错")
except ValueError as e:
    check("真内网 base_url 仍拒绝", "非公网" in str(e))

try:
    run(AgnesVideoGenerate().execute(
        prompt="x", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
        api_key="sk", base_url=agnes_api.BASE_URL_CN,
        media_mode={"media_mode": "全能参考"},
        media_files='[' + ','.join(f'{{"type": "image", "name": "_tmp_panel_{i}.png [input]"}}' for i in range(1, 10)) + ']',
    ))
    check("2.5 参考图超 8 张报错", False, "未抛错")
except ValueError as e:
    check("2.5 参考图超 8 张报错", "上限" in str(e))

# ------------------------------------------------------------ 11. video_from_file 真实调用链（此前被 stub 掩盖）
class _FakeVideoFromFile:
    calls = []

    def __init__(self, path):
        _FakeVideoFromFile.calls.append(path)


# 11.1 顶层直接导出（新版 ComfyUI 命中的路径）
latest_stub.VideoFromFile = _FakeVideoFromFile
_FakeVideoFromFile.calls.clear()
v = agnes_utils.video_from_file("a.mp4")
check("VideoFromFile 顶层导出直调", isinstance(v, _FakeVideoFromFile) and _FakeVideoFromFile.calls == ["a.mp4"])
del latest_stub.VideoFromFile

# 11.2 InputImpl 命名空间 fallback
class _InputImplNS:
    pass


latest_stub.InputImpl = _InputImplNS
_InputImplNS.VideoFromFile = _FakeVideoFromFile
_FakeVideoFromFile.calls.clear()
v2 = agnes_utils.video_from_file("b.mp4")
check("InputImpl 命名空间 fallback", isinstance(v2, _FakeVideoFromFile) and _FakeVideoFromFile.calls == ["b.mp4"])
del latest_stub.InputImpl

# 11.3 全部路径缺失 → ImportError
try:
    agnes_utils.video_from_file("c.mp4")
    check("全路径缺失报 ImportError", False, "未抛错")
except ImportError as e:
    check("全路径缺失报 ImportError", "VideoFromFile" in str(e))

# ------------------------------------------------------------ 12. 审查修复项回归
import os  # noqa: E402

_orig_time = agnes_api.time
try:
    client = agnes_api.AgnesClient("sk-test", agnes_api.BASE_URL_CN)

    # 12.1 _request 对 429 重试后成功
    agnes_api.time = types.SimpleNamespace(time=lambda: 0.0, sleep=lambda s: None)
    calls = {"n": 0}

    def flaky(method, url, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return FakeResponse({"error": "rate"}, status=429)
        return FakeResponse({"ok": True})

    agnes_api.requests.request = flaky
    r = client._request("GET", f"{agnes_api.BASE_URL_CN}/x")
    check("429 重试后成功", calls["n"] == 3 and r.json() == {"ok": True}, calls["n"])

    # 12.2 创建视频任务不重试（防重复建任务计费）
    calls["n"] = 0
    try:
        client.create_video({"model": "agnes-video-2.5"})
        check("创建任务 429 不重试", False, "未抛错")
    except agnes_api.AgnesApiError:
        pass
    check("创建任务仅请求 1 次", calls["n"] == 1, calls["n"])

    # 12.3 轮询状态序列 + 进度回调收到累计百分比
    seq = [
        FakeResponse({"status": "queued", "progress": 0}),
        FakeResponse({"status": "in_progress", "progress": 40}),
        FakeResponse({"status": "completed", "progress": 100, "url": "https://cdn.example.com/out.mp4"}),
    ]
    progress_calls = []
    agnes_api.requests.request = lambda method, url, **kw: seq.pop(0)
    data = client.poll_video("v1", "agnes-video-2.5", interval=0.0, progress_cb=progress_calls.append)
    check("轮询状态序列完成", data["status"] == "completed" and not seq)
    check("进度回调为累计百分比", progress_calls == [0, 40, 100], progress_calls)

    # 12.4 轮询遇 429 退避后继续（不终止任务）
    # 前 4 次 HTTP 均返回 429（覆盖 _request 两轮内部重试），第 5 次起成功——
    # 迫使 poll_video 外层退避两次（6s、12s 档）后再完成
    state = {"n": 0}
    sleeps = []

    def rate_then_ok(method, url, **kw):
        state["n"] += 1
        if state["n"] <= 4:
            return FakeResponse({"error": "rate"}, status=429)
        return FakeResponse({"status": "completed", "progress": 100, "url": "https://cdn.example.com/out.mp4"})

    agnes_api.requests.request = rate_then_ok
    agnes_api.time = types.SimpleNamespace(time=lambda: 0.0, sleep=lambda s: sleeps.append(s))
    data = client.poll_video("v1", "agnes-video-2.5", interval=3.0)
    check("429 退避后继续并完成", data["status"] == "completed")
    check("退避间隔拉长（3+4=7s / 3+8=11s 档）", 7 in sleeps and 11 in sleeps, sleeps)
    check("退避期间继续查询", state["n"] == 5, state["n"])  # 4 次 429 + 第 5 次成功

    # 12.5 轮询 200+非 JSON（网关挑战页）容错重查
    state["n"] = 0

    def html_then_ok(method, url, **kw):
        state["n"] += 1
        if state["n"] == 1:
            resp = FakeResponse({"html": True})
            resp.text = "<html>challenge</html>"
            resp.json = lambda: (_ for _ in ()).throw(ValueError("no json"))
            return resp
        return FakeResponse({"status": "completed", "progress": 100, "url": "https://cdn.example.com/out.mp4"})

    agnes_api.requests.request = html_then_ok
    data = client.poll_video("v1", "agnes-video-2.5", interval=3.0)
    check("非 JSON 响应容错重查", data["status"] == "completed")
finally:
    agnes_api.time = _orig_time
    agnes_api.requests.request = lambda method, url, **kw: (
        fake_post(url, **kw) if method == "POST" else fake_get(url, **kw)
    )

# 12.6 resolve_api_key 环境变量分支
os.environ["AGNES_API_KEY"] = "sk-env"
check("环境变量兜底", agnes_utils.resolve_api_key("") == "sk-env")
check("节点 key 优先于环境变量", agnes_utils.resolve_api_key("sk-node") == "sk-node")
os.environ["AGNES_API_KEY"] = "   "
try:
    agnes_utils.resolve_api_key("")
    check("空白环境变量视为未配置", False, "未抛错")
except ValueError:
    check("空白环境变量视为未配置", True)
os.environ.pop("AGNES_API_KEY", None)

# 12.7 全能参考只连音频时空数组不发送
captured.clear()
run(AgnesVideoGenerate().execute(
    prompt="跟随 <Audio 1> 的节奏", model="agnes-video-2.5", seconds="5", size="720P",
    aspect_ratio="16:9", api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
    media_mode={"media_mode": "全能参考"},
    media_files='[{"type": "audio", "name": "_tmp_test_audio.mp3 [input]"}]',
))
body = captured["json"]
check("仅音频时省略 images 字段", "images" not in body, body.get("images"))
check("audios 正常发送 1 条", len(body.get("audios", [])) == 1)

# 12.8 未知模式标签报错（防止旧工作流静默按文生视频计费）
try:
    run(AgnesVideoGenerate().execute(
        prompt="x", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
        api_key="sk", base_url=agnes_api.BASE_URL_CN,
        media_mode={"media_mode": "旧版标签"},
    ))
    check("未知模式标签报错", False, "未抛错")
except ValueError as e:
    check("未知模式标签报错", "合法值" in str(e))

# 12.9 keyframe 收到 0 帧 batch 时友好报错
try:
    run(AgnesVideoGenerate().execute(
        prompt="x", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
        api_key="sk", base_url=agnes_api.BASE_URL_CN,
        media_mode={"media_mode": "图生视频 / 首尾帧", "first_frame": types.SimpleNamespace(shape=(0, 8, 8, 3))},
    ))
    check("空 batch 友好报错", False, "未抛错")
except ValueError as e:
    check("空 batch 友好报错", "空 batch" in str(e))

# 12.10 下载重定向到内网被拒（SSRF 防护）
class _RedirectResp:
    status_code = 302
    headers = {"Location": "http://127.0.0.1/secret"}

    def close(self):
        pass

    def raise_for_status(self):
        pass


_orig_utils_get = agnes_utils.requests.get
try:
    agnes_utils.requests.get = lambda url, **kw: _RedirectResp()
    try:
        agnes_utils.download_file_to_bytes("https://cdn.example.com/file")
        check("重定向到内网被拒", False, "未抛错")
    except ValueError as e:
        check("重定向到内网被拒", "非公网" in str(e))
finally:
    agnes_utils.requests.get = _orig_utils_get

# 12.11 IPv4-mapped IPv6 环回地址拒绝（CVE-2024-4032 防护）
_orig_gai = agnes_utils.socket.getaddrinfo
try:
    agnes_utils.socket.getaddrinfo = lambda h, p=None: [(2, 1, 6, "", ("::ffff:127.0.0.1", 0))]
    try:
        agnes_utils.assert_public_http_url("https://rebind.example.com/v1")
        check("IPv4-mapped 环回拒绝", False, "未抛错")
    except ValueError as e:
        check("IPv4-mapped 环回拒绝", "非公网" in str(e))
finally:
    agnes_utils.socket.getaddrinfo = _orig_gai

# 12.12 进度条用绝对值语义（update_absolute）
BAR_CALLS.clear()
captured.clear()
run(AgnesVideoGenerate().execute(
    prompt="进度语义验证", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
    api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
    media_mode={"media_mode": "文生视频"},
))
check("进度条走 update_absolute", BAR_CALLS == [100], BAR_CALLS)

# ------------------------------------------------------------ 13. 全能参考本地上传音频
ref_opt_inputs = {i.name for i in ref_opt.inputs}

# 13.1 本地上传文件 → Data URI 直传
try:
    captured.clear()
    run(AgnesVideoGenerate().execute(
        prompt="跟随 <Audio 1> 的节奏", model="agnes-video-2.5", seconds="5", size="720P",
        aspect_ratio="16:9", api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
        media_mode={"media_mode": "全能参考"},
        media_files='[{"type": "audio", "name": "_tmp_test_audio.mp3 [input]"}]',
    ))
    body = captured["json"]
    check("本地上传音频转 Data URI 发送",
          body.get("audios") == [f"data:audio/mpeg;base64,{base64.b64encode(b'ID3fake mp3 bytes for test').decode()}"],
          body.get("audios"))

    # 13.2 上传 + URL 混用合并计数
    captured.clear()
    run(AgnesVideoGenerate().execute(
        prompt="混用音频", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
        api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
        media_mode={"media_mode": "全能参考"},
        media_files='[{"type": "audio", "name": "_tmp_test_audio.mp3 [input]"}]',
    ))
    body = captured["json"]
    check("面板音频 Data URI 发送",
      body.get("audios") == [f"data:audio/mpeg;base64,{base64.b64encode(b'ID3fake mp3 bytes for test').decode()}"],
      body.get("audios"))

    # 13.3 上传槽选「（不使用）」时不贡献（仅 URL 一条）
    captured.clear()
    run(AgnesVideoGenerate().execute(
        prompt="无本地上传", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
        api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
        media_mode={"media_mode": "全能参考"},
        media_files=(
            '[{"type": "audio", "name": "（不使用） [input]"},'
            ' {"type": "audio", "name": "_tmp_test_audio.mp3 [input]"}]'
        ),
    ))
    body = captured["json"]
    check("（不使用）素材被忽略，有效音频 1 条",
          body.get("audios") == [f"data:audio/mpeg;base64,{base64.b64encode(b'ID3fake mp3 bytes for test').decode()}"],
          body.get("audios"))
finally:
    pass

# ------------------------------------------------------------ 14. 素材面板：视频上传（media_files JSON）
try:
    captured.clear()
    run(AgnesVideoGenerate().execute(
        prompt="参考 <Video 1> 的镜头运动", model="agnes-video-2.5", seconds="5", size="720P",
        aspect_ratio="16:9", api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
        media_mode={"media_mode": "全能参考"},
        media_files='[{"type": "video", "name": "_tmp_test_video.mp4 [input]"}]',
    ))
    body = captured["json"]
    expected_uri = f"data:video/mp4;base64,{base64.b64encode(b'ftypfake mp4 bytes').decode()}"
    check("面板上传视频转 Data URI 直传",
          body.get("videos") == [{"url": expected_uri, "start_seconds": 0, "require_audio": False}],
          body.get("videos"))

    # 14.2 flash 拒绝视频参考（上传）
    try:
        run(AgnesVideoGenerate().execute(
            prompt="x", model="agnes-video-2.5-flash", seconds="5", size="720P", aspect_ratio="16:9",
            api_key="sk", base_url=agnes_api.BASE_URL_CN,
            media_mode={"media_mode": "全能参考"},
            media_files='[{"type": "video", "name": "_tmp_test_video.mp4 [input]"}]',
        ))
        check("flash 拒绝面板上传视频", False, "未抛错")
    except ValueError as e:
        check("flash 拒绝面板上传视频", "视频参考" in str(e) or "video_url" in str(e))

    # 14.2b flash 非参考模式：面板残留视频应被忽略，不再误报（回归）
    captured.clear()
    run(AgnesVideoGenerate().execute(
        prompt="纯文生视频", model="agnes-video-2.5-flash", seconds="5", size="720P", aspect_ratio="16:9",
        api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
        media_mode={"media_mode": "文生视频"},
        media_files='[{"type": "video", "name": "_tmp_test_video.mp4 [input]"}]',
    ))
    body = captured["json"]
    check("flash 文生视频忽略面板残留视频",
          body.get("mode") == "text" and "videos" not in body, body.get("mode"))

    # 14.3 上传与 URL 同时提供 → 报错
    try:
        run(AgnesVideoGenerate().execute(
            prompt="x", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
            api_key="sk", base_url=agnes_api.BASE_URL_CN,
            media_mode={"media_mode": "全能参考"},
            media_files='[{"type": "video", "name": "_tmp_test_video.mp4 [input]"}]',
            video_url="https://cdn.example.com/ref.mp4",
        ))
        check("视频参考重复提供报错", False, "未抛错")
    except ValueError as e:
        check("视频参考重复提供报错", "二选一" in str(e))

    # 14.4 video_url 仍正常（既有链路回归）
    captured.clear()
    run(AgnesVideoGenerate().execute(
        prompt="参考 <Video 1> 的镜头节奏", model="agnes-video-2.5", seconds="5", size="720P",
        aspect_ratio="21:9", api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
        media_mode={"media_mode": "全能参考"},
        video_url="https://cdn.example.com/ref.mp4",
    ))
    body = captured["json"]
    check("video_url 仍组装 videos 数组",
          body.get("videos") == [{"url": "https://cdn.example.com/ref.mp4", "start_seconds": 0, "require_audio": False}],
          body.get("videos"))
finally:
    pass

# ------------------------------------------------------------ 15. 音/视频超限自动压缩
_orig_vt = agnes_utils._transcode_video
_orig_at = agnes_utils._transcode_audio
_orig_vmax = agnes_utils.MAX_VIDEO_FILE_BYTES
_orig_amax = agnes_utils.MAX_AUDIO_FILE_BYTES
_TEST_AUDIO.write_bytes(b"ID3fake mp3 bytes for test")
_TEST_VIDEO.write_bytes(b"ftypfake mp4 bytes")
try:
    # 15.1 视频超限 → 压缩管线触发，输出 mp4 Data URI（源文件 18B，上限压到 10B 触发）
    agnes_utils.MAX_VIDEO_FILE_BYTES = 10
    agnes_utils._transcode_video = lambda path, crf, scale: b"x" * 8  # 压缩后 8B 达标
    uri = agnes_utils.video_file_to_data_uri(str(_TEST_VIDEO), label="测试视频")
    check("视频超限自动压缩为 mp4 Data URI", uri.startswith("data:video/mp4;base64,"), uri[:40])

    # 15.2 全档仍超限 → 报错
    agnes_utils._transcode_video = lambda path, crf, scale: b"x" * 20
    try:
        agnes_utils.video_file_to_data_uri(str(_TEST_VIDEO), label="测试视频")
        check("视频全档超限报错", False, "未抛错")
    except ValueError as e:
        check("视频全档超限报错", "50MB" in str(e))

    # 15.3 压缩器异常 → 友好报错（模拟环境缺编码器）
    def _boom(path, crf, scale):
        raise RuntimeError("no libx264")

    agnes_utils._transcode_video = _boom
    try:
        agnes_utils.video_file_to_data_uri(str(_TEST_VIDEO), label="测试视频")
        check("压缩器异常友好报错", False, "未抛错")
    except ValueError as e:
        check("压缩器异常友好报错", "自动压缩失败" in str(e))

    # 15.4 音频超限 → AAC 阶梯压缩
    agnes_utils.MAX_AUDIO_FILE_BYTES = 10
    agnes_utils._transcode_audio = lambda path, kbps: b"a" * 8
    uri = agnes_utils.audio_file_to_data_uri(str(_TEST_AUDIO), label="测试音频")
    check("音频超限自动压缩为 AAC Data URI", uri.startswith("data:audio/mp4;base64,"), uri[:40])

    # 15.5 音频第一档就达标（如 WAV 转一次 AAC 即可）
    agnes_utils._transcode_audio = lambda path, kbps: b"a" * (9 if kbps == 192 else 5)
    uri = agnes_utils.audio_file_to_data_uri(str(_TEST_AUDIO), label="测试音频")
    check("音频逐级降码率达标", uri.startswith("data:audio/mp4;base64,"), uri[:40])
finally:
    agnes_utils._transcode_video = _orig_vt
    agnes_utils._transcode_audio = _orig_at
    agnes_utils.MAX_VIDEO_FILE_BYTES = _orig_vmax
    agnes_utils.MAX_AUDIO_FILE_BYTES = _orig_amax
    agnes_utils._frame_to_image = lambda frame: Image.new("RGB", (256, 256))
    agnes_utils._encode_image_auto = lambda img, label="参考图": (_REAL_PNG, "image/png")

# ------------------------------------------------------------ 16. 素材面板图片 + 双连线口合并
try:
    # 16.1 面板上传 1 图 + 两个连线口 3 帧 = 4 张
    captured.clear()
    run(AgnesVideoGenerate().execute(
        prompt="以 <Picture 1> 的形象为准", model="agnes-video-2.5", seconds="5", size="720P",
        aspect_ratio="16:9", api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
        media_mode={
            "media_mode": "全能参考",
            "reference_images": _StubBatch(1),
            "reference_images_2": _StubBatch(2),
        },
        media_files='[{"type": "image", "name": "_tmp_panel_1.png [input]"}]',
    ))
    body = captured["json"]
    check("面板图直传 1 张 Data URI",
          len(body.get("images", [])) == 1 and body["images"][0].startswith("data:image/png;base64,"),
          len(body.get("images", [])))

    # 16.2 flash 合并超 5 张报错
    try:
        run(AgnesVideoGenerate().execute(
            prompt="x", model="agnes-video-2.5-flash", seconds="5", size="720P", aspect_ratio="16:9",
            api_key="sk", base_url=agnes_api.BASE_URL_CN,
            media_mode={"media_mode": "全能参考"},
            media_files='[' + ','.join(f'{{"type": "image", "name": "_tmp_panel_{i}.png [input]"}}' for i in range(1, 7)) + ']',
        ))
        check("flash 参考图超限报错", False, "未抛错")
    except ValueError as e:
        check("flash 参考图超限报错", "上限" in str(e))


    # 16.4 面板上传 8 图 + 连线 1 帧 = 9 张，超出 8 报错
    many = ",".join(f'{{"type": "image", "name": "_tmp_panel_{i}.png [input]"}}' for i in range(9))
    try:
        run(AgnesVideoGenerate().execute(
            prompt="x", model="agnes-video-2.5", seconds="5", size="720P", aspect_ratio="16:9",
            api_key="sk", base_url=agnes_api.BASE_URL_CN,
            media_mode={
                "media_mode": "全能参考",
                "reference_images": _StubBatch(1),
            },
            media_files=f'[{many}]',
        ))
        check("参考图合计超 8 报错", False, "未抛错")
    except ValueError as e:
        check("参考图合计超 8 报错", "上限" in str(e))
finally:
    pass

# ------------------------------------------------------------ 17. 参考图边长自动适配（256–5760px）
# 17.1 单元：小图放大 / 大图缩小 / 合规不变 / 极端比例报错
small = agnes_utils._fit_side_limits(Image.new("RGB", (100, 60)), "小图")
check("边长 <256 自动放大（短边达 256）", small.size == (427, 256), small.size)
huge = agnes_utils._fit_side_limits(Image.new("RGB", (6000, 3000)), "大图")
check("边长 >5760 自动缩小", huge.size == (5760, 2880), huge.size)
ok_size = agnes_utils._fit_side_limits(Image.new("RGB", (1024, 768)), "合规图")
check("合规尺寸不变", ok_size.size == (1024, 768), ok_size.size)
try:
    agnes_utils._fit_side_limits(Image.new("RGB", (5760, 100)), "长条图")
    check("极端宽高比报错", False, "未抛错")
except ValueError as e:
    check("极端宽高比报错", "宽高比" in str(e))

# 17.2 上传槽小图自动放大（真实 PIL 编码链路）
_TEST_SMALL = Path(__file__).resolve().parent / "_tmp_small_ref.png"
Image.new("RGB", (100, 60)).save(_TEST_SMALL, format="PNG")
try:
    uri = agnes_utils.image_file_to_data_uri(str(_TEST_SMALL), label="小图上传")
    import base64 as _b64
    decoded = Image.open(io.BytesIO(_b64.b64decode(uri.split(",", 1)[1])))
    check("上传小图自动放大到 256", decoded.size == (427, 256), decoded.size)
finally:
    _TEST_SMALL.unlink(missing_ok=True)

# ------------------------------------------------------------ 18. 轮询状态/间隔优化
import time as _time
_orig_time_mod = agnes_api.time
try:
    # 18.1 pending 为等待类状态：不刷未知日志、继续到完成
    seq = [
        FakeResponse({"status": "pending", "progress": 0}),
        FakeResponse({"status": "pending", "progress": 0}),
        FakeResponse({"status": "in_progress", "progress": 50}),
        FakeResponse({"status": "completed", "progress": 100, "url": "https://cdn.example.com/out.mp4"}),
    ]
    agnes_api.requests.request = lambda method, url, **kw: seq.pop(0)
    client = agnes_api.AgnesClient("sk-test", agnes_api.BASE_URL_CN)
    agnes_api.time = types.SimpleNamespace(time=lambda: 0.0, sleep=lambda s: None)
    data = client.poll_video("v1", "agnes-video-2.5", interval=0.0)
    check("pending 状态静默等待至完成", data["status"] == "completed" and not seq)

    # 18.2 未知状态只提示一次
    seq = [
        FakeResponse({"status": "weird_state"}),
        FakeResponse({"status": "weird_state"}),
        FakeResponse({"status": "completed", "progress": 100, "url": "https://cdn.example.com/out.mp4"}),
    ]
    agnes_api.requests.request = lambda method, url, **kw: seq.pop(0)
    import io as _io
    import contextlib
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        data = client.poll_video("v1", "agnes-video-2.5", interval=0.0)
    logs = buf.getvalue()
    check("未知状态只提示一次", logs.count("未知") == 1, logs)
finally:
    agnes_api.time = _orig_time_mod
    agnes_api.requests.request = lambda method, url, **kw: (
        fake_post(url, **kw) if method == "POST" else fake_get(url, **kw)
    )

# ------------------------------------------------------------ 19. 轮询失败/超时的错误类型
# 回归：这两条分支曾把 AgnesApiError 误写成 AgensApiError，运行时抛 NameError
# 而非预期的中文报错，且丢掉服务端返回的失败原因
_orig_time_mod19 = agnes_api.time
try:
    client19 = agnes_api.AgnesClient("sk-test", agnes_api.BASE_URL_CN)

    # 19.1 任务 failed → AgnesApiError（带服务端 error 文案），不是 NameError
    agnes_api.requests.request = lambda method, url, **kw: FakeResponse(
        {"status": "failed", "error": "content policy violation"}
    )
    agnes_api.time = types.SimpleNamespace(time=lambda: 0.0, sleep=lambda s: None)
    try:
        client19.poll_video("v1", "agnes-video-2.5", interval=0.0)
        check("任务 failed 抛 AgnesApiError", False, "未抛错")
    except agnes_api.AgnesApiError as e:
        check("任务 failed 抛 AgnesApiError", "content policy violation" in str(e), str(e))

    # 19.2 轮询超时 → AgnesApiError（不是 NameError）
    agnes_api.requests.request = lambda method, url, **kw: FakeResponse({"status": "in_progress"})
    clock19 = {"t": 0.0}

    def _tick19():
        clock19["t"] += 5.0
        return clock19["t"]

    agnes_api.time = types.SimpleNamespace(time=_tick19, sleep=lambda s: None)
    try:
        client19.poll_video("v1", "agnes-video-2.5", interval=0.0, timeout=1)
        check("轮询超时抛 AgnesApiError", False, "未抛错")
    except agnes_api.AgnesApiError as e:
        check("轮询超时抛 AgnesApiError", "轮询超时" in str(e), str(e))
finally:
    agnes_api.time = _orig_time_mod19
    agnes_api.requests.request = lambda method, url, **kw: (
        fake_post(url, **kw) if method == "POST" else fake_get(url, **kw)
    )

# ------------------------------------------------------------ 20. 图像节点自动落盘（saved_path 输出口）
import shutil  # noqa: E402
_orig_save_png = agnes_image_mod._save_png
try:
    # 20.1 正常路径：文件写入 output/AgnesAI，输出相对路径 + ui 图片条目
    shutil.rmtree(_SAVE_ROOT, ignore_errors=True)
    captured.clear()
    result = run(AgnesImageGenerate().execute(
        prompt="落盘验证", model="agnes-image-2.5-flash", size="1K", ratio="1:1",
        api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
    ))
    out, rel_path = result.outputs
    file = _SAVE_ROOT / "AgnesAI" / (rel_path.split("/", 1)[1] if "/" in rel_path else rel_path)
    check("落盘后输出相对路径", rel_path.startswith("AgnesAI/") and file.exists(), (rel_path, file.exists()))
    check("落盘文件内容与生成 PNG 一致", file.read_bytes() == _REAL_PNG, len(file.read_bytes()))
    check("UI 输出 images 条目指向 output 类型",
          result.ui == {"images": [{"filename": file.name, "subfolder": "AgnesAI", "type": "output"}]},
          result.ui)
    check("IMAGE 输出不受落盘影响", out == ("IMAGE_TENSOR",), out)

    # 20.2 连续两次生成文件名不重复（递增序号防覆盖）
    run(AgnesImageGenerate().execute(
        prompt="落盘验证二", model="agnes-image-2.5-flash", size="1K", ratio="1:1",
        api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
    ))
    check("第二次落盘文件名递增不覆盖", len(list((_SAVE_ROOT / "AgnesAI").glob("*.png"))) == 2)

    # 20.3 落盘失败：不影响出图，saved_path 为空串、无 ui 图片
    def _boom(prefix, output_dir, *args, **kwargs):
        raise OSError("disk full")

    fp_stub.get_save_image_path = _boom
    try:
        captured.clear()
        result = run(AgnesImageGenerate().execute(
            prompt="落盘失败验证", model="agnes-image-2.5-flash", size="1K", ratio="1:1",
            api_key="sk-test", base_url=agnes_api.BASE_URL_CN,
        ))
        out, rel_path = result.outputs
        check("落盘失败 saved_path 为空串", rel_path == "", rel_path)
        check("落盘失败不构造 ui 图片", result.ui is None, result.ui)
        check("落盘失败仍返回 IMAGE", out == ("IMAGE_TENSOR",), out)
    finally:
        fp_stub.get_save_image_path = _stub_get_save_image_path

    # 20.4 单元：_save_png 直接调用（文件字节不合法也照写，落盘不校验内容）
    rel, ui = agnes_image_mod._save_png(b"not-a-png")
    written = _SAVE_ROOT / "AgnesAI" / (rel.split("/", 1)[1] if "/" in rel else rel)
    check("_save_png 单元写入并返回 ui", written.read_bytes() == b"not-a-png"
          and ui and ui["type"] == "output", (rel, ui))
finally:
    agnes_image_mod._save_png = _orig_save_png
    shutil.rmtree(_SAVE_ROOT, ignore_errors=True)

# ------------------------------------------------------------ 21. 排队阶段进度按等待时长估算（单调不回退）
# 服务端未返回 progress 字段时，poll_video 按已等待时长估算 0–30% 上报
_orig_time_mod21 = agnes_api.time
try:
    client21 = agnes_api.AgnesClient("sk-test", agnes_api.BASE_URL_CN)

    # 21.1 排队期无 progress 字段 → 随等待时长递增上报估算值
    seq = [
        FakeResponse({"status": "queued"}),
        FakeResponse({"status": "queued"}),
        FakeResponse({"status": "queued"}),
        FakeResponse({"status": "completed", "progress": 100, "url": "https://cdn.example.com/out.mp4"}),
    ]
    agnes_api.requests.request = lambda method, url, **kw: seq.pop(0)
    clock = {"t": 0.0}

    def _tick21(delta):
        clock["t"] += delta
        return clock["t"]

    progress_calls21 = []
    agnes_api.time = types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: _tick21(s))
    client21.poll_video("v1", "agnes-video-2.5", interval=20.0, timeout=200,
                        progress_cb=progress_calls21.append)
    # 三次排队查询依次在 0s / 20s / 40s 上报：0、3、6（估算区间 0–30 内单调递增）
    queued_vals = progress_calls21[:-1]
    check("排队阶段估算进度递增", queued_vals == [0, 3, 6], queued_vals)
    check("估算值落在 0–30 区间", all(0 <= v <= 30 for v in queued_vals), queued_vals)
    check("完成后最终上报 100", progress_calls21[-1] == 100, progress_calls21)

    # 21.2 服务端返回的真实 progress 低于估算值 → 不回退（保持单调）
    seq = [
        FakeResponse({"status": "queued"}),                     # 0s → 估算 0
        FakeResponse({"status": "in_progress", "progress": 1}), # 真实 1 > 0，上报 1
        FakeResponse({"status": "in_progress", "progress": 0}), # 真实 0 < 已上报 1 → 不回退
        FakeResponse({"status": "completed", "progress": 100, "url": "https://cdn.example.com/out.mp4"}),
    ]
    agnes_api.requests.request = lambda method, url, **kw: seq.pop(0)
    clock["t"] = 0.0
    progress_calls21 = []
    agnes_api.time = types.SimpleNamespace(time=lambda: clock["t"], sleep=lambda s: _tick21(s))
    client21.poll_video("v1", "agnes-video-2.5", interval=0.0, timeout=200,
                        progress_cb=progress_calls21.append)
    check("真实 progress 低于估算不回退", progress_calls21 == [0, 1, 100], progress_calls21)
finally:
    agnes_api.time = _orig_time_mod21
    agnes_api.requests.request = lambda method, url, **kw: (
        fake_post(url, **kw) if method == "POST" else fake_get(url, **kw)
    )

# ------------------------------------------------------------ 99. 清理临时素材
for _p in ("_tmp_test_audio.mp3", "_tmp_test_video.mp4", "_tmp_panel_1.png", "_tmp_panel_2.png"):
    (_TMP_DIR / _p).unlink(missing_ok=True)

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：{FAILS}")
    sys.exit(1)
print("✅ 全部干跑测试通过")
