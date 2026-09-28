"""工具函数：图像编解码、URL 安全校验、文件下载、ComfyUI VIDEO 类型兼容导入。"""

import base64
import ipaddress
import os
import re
import socket
import tempfile
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests

try:
    import torch
except ImportError:  # 允许在无 torch 的环境下做 payload 干跑测试
    torch = None

from PIL import Image

# Agnes API：单张参考图 <15MB（服务端按十进制 MB 卡口）；base64 膨胀 1/3，
# 编码后字节收口 10.5MiB → base64 后 ≈14MiB（14.68 十进制 MB），留足余量
MAX_REF_IMAGE_BYTES = (10 * 1024 + 512) * 1024

# Agnes 视频 API：单条参考音频 <15MB（超限自动转 AAC 并逐级降码率压缩）
MAX_AUDIO_FILE_BYTES = 15 * 1024 * 1024

# Agnes 视频 API：单个参考视频 <50MB、时长 2-12 秒
MAX_VIDEO_FILE_BYTES = 50 * 1024 * 1024

# 音频扩展名 → Data URI mime
_AUDIO_MIME = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".flac": "audio/flac",
    ".opus": "audio/opus",
    ".webm": "audio/webm",
    ".wma": "audio/x-ms-wma",
}

# 视频扩展名 → Data URI mime
_VIDEO_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
}

# 下载重定向手动跟跳上限：每跳都重新做公网校验，防止重定向绕过 SSRF 防护
_MAX_REDIRECTS = 5

# RFC 2544 / RFC 5180 基准测试段：Clash/Surge 等 fake-ip 模式代理惯用的虚拟 IP 段，
# 由本机透明代理转发到真实目标、不指向任何真实内网服务，视为公网出站处理
_PROXY_FAKEIP_NETWORKS = [
    ipaddress.ip_network("198.18.0.0/15"),  # IPv4 benchmarking
    ipaddress.ip_network("2001:2::/48"),    # IPv6 benchmarking
]


def resolve_api_key(value: str) -> str:
    key = (value or "").strip() or os.environ.get("AGNES_API_KEY", "").strip()
    if not key:
        raise ValueError(
            "未配置 Agnes AI API Key：请在节点 api_key 中填入，"
            "或设置环境变量 AGNES_API_KEY。密钥在 Agnes AI 开发者控制台获取。"
        )
    return key


def _frame_to_image(frame):
    """单帧 [H, W, C] float tensor (0-1) → PIL Image。"""
    import numpy as np

    arr = frame.detach().cpu().clamp(0, 1).mul(255).round().to(torch.uint8).numpy()
    return Image.fromarray(arr)


def _flatten_alpha(img):
    """带透明通道的图合成到白底后转 RGB（JPEG 不支持 alpha）。"""
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _downscale(img, max_side: int):
    """等比缩小到最长边不超过 max_side（LANCZOS，保细节），无需缩小时原样返回。"""
    w, h = img.size
    longest = max(w, h)
    if longest <= max_side:
        return img
    scale = max_side / longest
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def _encode_image(img, fmt: str, **kw) -> bytes:
    buf = BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


# Agnes API 参考图边长范围（像素）：小于 256 或大于 5760 会被 400 拒绝
MIN_MEDIA_SIDE = 256
MAX_MEDIA_SIDE = 5760


def _fit_side_limits(img, label: str, desc: str = ""):
    """把图片等比调整到参考图边长范围（256–5760px）：过小自动放大、过大自动缩小。

    宽高比超过 MAX/MIN（约 22.5:1）的极端长条图无法同时满足两边，直接报错引导裁剪。
    """
    w, h = img.size
    longest, shortest = max(w, h), min(w, h)
    if longest / shortest > MAX_MEDIA_SIDE / MIN_MEDIA_SIDE:
        raise ValueError(
            f"{label}{desc} 宽高比过大（{w}x{h}），无法满足参考图边长 256–5760px 要求，"
            "请先适当裁剪后再使用。"
        )
    scale = 1.0
    if longest > MAX_MEDIA_SIDE:
        scale = MAX_MEDIA_SIDE / longest
    if shortest * scale < MIN_MEDIA_SIDE:
        scale = MIN_MEDIA_SIDE / shortest
    if scale == 1.0:
        return img
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    action = "放大" if scale > 1 else "缩小"
    print(f"[Agnes AI] {label}{desc} {w}x{h} 边长超出参考图范围（256–5760px），已自动{action}至 {new_w}x{new_h}")
    return resized


def _encode_image_auto(img, label: str = "参考图") -> tuple[bytes, str]:
    """把 PIL 图像编码为不超过大小上限的字节流，尽量保留原图质量。

    渐进策略（每步达标即返回，优先无损/高保真）：
    1. PNG 直出（无损）；
    2. JPEG quality=95、4:4:4 色度、原图分辨率（视觉上几乎无损）；
    3. 逐级降低质量/降采样（3072 → 2048 → 1536 → 1024）。
    返回 (字节流, mime)，全部超限时才报错。
    """
    orig_w, orig_h = img.size

    png = _encode_image(img, "PNG")
    if len(png) <= MAX_REF_IMAGE_BYTES:
        return png, "image/png"

    rgb = _flatten_alpha(img)
    for quality, subsampling, max_side in _JPEG_STEPS:
        im = rgb if max_side is None else _downscale(rgb, max_side)
        data = _encode_image(im, "JPEG", quality=quality, subsampling=subsampling, optimize=True)
        if len(data) <= MAX_REF_IMAGE_BYTES:
            side_desc = "原图分辨率" if max_side is None else f"长边≤{max_side}"
            print(
                f"[Agnes AI] {label}（{orig_w}x{orig_h}）PNG {len(png) / 1048576:.1f}MB 超出限制，"
                f"已自动压缩：JPEG q{quality} / {side_desc} → {len(data) / 1048576:.1f}MB（尽量保留画质）"
            )
            return data, "image/jpeg"

    raise ValueError(
        f"{label}（{orig_w}x{orig_h}）压缩到最小档仍超过 Agnes API 单张 15MB 限制，"
        "请手动缩小图片分辨率后重试。"
    )


def encode_frame_auto(frame, label: str = "参考图") -> tuple[bytes, str]:
    """单帧 tensor → 自动尺寸/体积适配的图像字节流（见 _encode_image_auto）。"""
    return _encode_image_auto(_frame_to_image(frame), label)


def tensor_to_data_uris(images, max_images: int | None = None, label: str = "image") -> list[str]:
    """IMAGE batch tensor → 每帧一个 Data URI（尺寸与体积自动适配）；超过 max_images 截断并提示。"""
    if images is None:
        return []
    total = images.shape[0]
    count = total if max_images is None else min(total, max_images)
    uris = []
    for i in range(count):
        img = _fit_side_limits(_frame_to_image(images[i]), label, f" 第 {i + 1} 张")
        data, mime = _encode_image_auto(img, label)
        uris.append(f"data:{mime};base64," + base64.b64encode(data).decode("ascii"))
    if max_images is not None and total > max_images:
        print(f"[Agnes AI] {label} 输入 {total} 张，超出上限 {max_images}，仅使用前 {max_images} 张。")
    return uris


def png_bytes_to_tensor(png: bytes):
    """PNG 字节流 → [1, H, W, C] float tensor，与 ComfyUI IMAGE 约定一致。"""
    import numpy as np

    img = Image.open(BytesIO(png)).convert("RGB")
    arr = np.asarray(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr).unsqueeze(0)


def assert_public_http_url(url: str, field: str = "url") -> str:
    """出站 URL 安全校验：仅允许 http/https，且 host 必须解析到公网地址。"""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{field} 仅支持 http/https 协议：{url!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"{field} 缺少主机名：{url!r}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"{field} 主机解析失败：{host}（{e}）")
    seen = False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        # IPv4-mapped IPv6（如 ::ffff:127.0.0.1）解包后按 IPv4 判断，
        # 避免未打 CVE-2024-4032 补丁的旧 Python 将其误判为公网
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        seen = True
        if any(addr in net for net in _PROXY_FAKEIP_NETWORKS):
            # fake-ip 虚拟段（IPv4 RFC2544 / IPv6 RFC5180 benchmarking）：由本机透明代理
            # 转发到真实目标，不指向任何真实内网服务，跳过内网判断予以放行
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_link_local
            or addr.is_unspecified
            or addr in ipaddress.ip_network("100.64.0.0/10")
        ):
            raise ValueError(
                f"{field} 拒绝非公网地址：{host} -> {addr}。"
                "若你确需访问内网网关，请自行修改 nodes/utils.py 的 assert_public_http_url。"
            )
    if not seen:
        raise ValueError(f"{field} 主机无可解析地址：{host}")
    return url


def _get_public_url(url: str, field: str, timeout):
    """禁用 requests 自动重定向，手动跟跳并在每一跳重新做公网校验。

    若允许 requests 盲跟重定向，恶意服务可用 302 把请求引到 127.0.0.1/云元数据等
    内网目标绕过首跳校验（SSRF）。残余风险：校验与连接之间存在极窄的 DNS
    rebinding 窗口（TTL=0 域名），对本地单机工具可接受。
    """
    resp = requests.get(url, allow_redirects=False, timeout=timeout)
    for _ in range(_MAX_REDIRECTS):
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        loc = resp.headers.get("Location")
        if not loc:
            return resp
        resp.close()
        url = assert_public_http_url(urljoin(url, loc), field=field)
        resp = requests.get(url, allow_redirects=False, timeout=timeout)
    resp.close()
    raise RuntimeError(f"重定向次数超过 {_MAX_REDIRECTS}，已中止：{url}")


def download_file_to_bytes(url: str, retries: int = 3) -> bytes:
    url = assert_public_http_url(url, field="下载 url")
    last = None
    for attempt in range(retries):
        try:
            resp = _get_public_url(url, field="下载 url", timeout=(10, 120))
            with resp:
                resp.raise_for_status()
                return resp.content
        except requests.RequestException as e:
            last = e
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"下载失败（已尝试 {retries} 次）{url}：{last}")


def _safe_temp_file(temp_dir: str, prefix: str, suffix: str) -> Path:
    """在 temp_dir 下创建安全临时文件：目录先规范化校验，文件名由 tempfile 生成。"""
    base = os.path.realpath(temp_dir)
    os.makedirs(base, exist_ok=True)
    safe_prefix = re.sub(r"[^A-Za-z0-9_]", "", prefix) or "agnes"
    fd, name = tempfile.mkstemp(suffix=suffix, prefix=f"{safe_prefix}_", dir=base)
    os.close(fd)
    path = Path(name).resolve()
    if not str(path).startswith(str(Path(base).resolve()) + os.sep):
        raise ValueError(f"临时文件路径越界：{path}")
    return path


def download_video_to_temp(url: str, temp_dir: str, prefix: str = "agnes_video", retries: int = 3) -> str:
    """下载 mp4 到 temp 目录并返回本地路径（URL 及每跳重定向都过公网校验）。"""
    url = assert_public_http_url(url, field="视频下载 url")
    path = _safe_temp_file(temp_dir, prefix, ".mp4")
    last = None
    for attempt in range(retries):
        try:
            resp = _get_public_url(url, field="视频下载 url", timeout=(10, 300))
            with resp:
                resp.raise_for_status()
                path.write_bytes(resp.content)
            return str(path)
        except requests.RequestException as e:
            last = e
            # 清掉半截/0 字节残留，避免污染 temp 目录
            Path(path).unlink(missing_ok=True)
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 8))
    Path(path).unlink(missing_ok=True)
    raise RuntimeError(f"视频下载失败（已尝试 {retries} 次）{url}：{last}")


def _transcode_audio(path: str, kbps: int) -> bytes:
    """用 PyAV 把音频转码为 AAC（指定码率），输出 audio-only MP4 容器。"""
    import av

    src = av.open(path)
    astream = src.streams.audio[0]
    rate = astream.codec_context.sample_rate

    buf = BytesIO()
    out = av.open(buf, "w", format="mp4")
    ostream = out.add_stream("aac", rate=rate)
    ostream.bit_rate = kbps * 1000

    resampler = av.AudioResampler(format="fltp", layout="stereo", rate=rate)
    for frame in src.decode(audio=0):
        for rf in resampler.resample(frame):
            for packet in ostream.encode(rf):
                out.mux(packet)
    for packet in ostream.encode():
        out.mux(packet)
    out.close()
    src.close()
    return buf.getvalue()


_JPEG_STEPS = [
    (95, 0, None),    # 原分辨率 + 4:4:4，最高保真
    (95, 0, 3072),
    (92, 0, 3072),
    (92, 0, 2048),
    (88, 2, 1536),
    (85, 2, 1024),
]

# 图片扩展名 → Data URI mime（未知格式会经 PIL 解码转 JPEG）
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def image_file_to_data_uri(path: str, label: str = "参考图") -> str:
    """本地上传的图片文件 → Data URI；超限自动压缩、尺寸自动适配（尽量保留画质）。

    原样直传优先（无损保真）；边长超出 256–5760px 时等比缩放，体积超过 15MB
    或为不支持的格式时，经 PIL 解码后走与参考图一致的 JPEG 渐进压缩阶梯。
    """
    file = Path(path)
    mime = _IMAGE_MIME.get(file.suffix.lower())
    try:
        data = file.read_bytes()
    except OSError as e:
        raise ValueError(f"{label} 读取失败：{path}（{e}）") from e
    try:
        img = Image.open(BytesIO(data))
        img.load()
    except Exception as e:
        raise ValueError(
            f"{label} 无法解码：{path}（{e}）。请使用 png/jpg/webp 等常见图片格式。"
        ) from e

    fitted = _fit_side_limits(img, label)
    size_changed = fitted is not img

    if not size_changed and mime and len(data) <= MAX_REF_IMAGE_BYTES:
        return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")

    # 需要重编码（尺寸调整 / 体积超限 / 未知格式）
    png = _encode_image(fitted, "PNG")
    if len(png) <= MAX_REF_IMAGE_BYTES:
        # 尺寸被调整或格式转换时，PNG 无损重编码最优
        return "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    for quality, subsampling, max_side in _JPEG_STEPS:
        im = fitted if max_side is None else _downscale(fitted, max_side)
        out = _encode_image(im, "JPEG", quality=quality, subsampling=subsampling, optimize=True)
        if len(out) <= MAX_REF_IMAGE_BYTES:
            side = "原图分辨率" if max_side is None else f"长边≤{max_side}"
            print(
                f"[Agnes AI] {label}（{img.size[0]}x{img.size[1]}）超出限制，已自动压缩："
                f"JPEG q{quality} / {side} → {len(out) / 1048576:.1f}MB"
            )
            return "data:image/jpeg;base64," + base64.b64encode(out).decode("ascii")
    raise ValueError(f"{label} 压缩到最小档仍超过 15MB，请手动缩小图片后重试。")


_AUDIO_COMPRESS_STEPS = [192, 128, 96, 64]  # AAC 码率阶梯（kbps），192k 听感几乎无损


def audio_file_to_data_uri(path: str, label: str = "音频") -> str:
    """本地音频文件 → data:audio/*;base64 Data URI（随请求直传）。

    超过 15MB 时自动压缩（尽量保留音质）：转 AAC 192k 起步，逐级降码率。
    Agnes 视频 API 对参考音频仅描述"可公开访问的 URL"，但图片/视频的 Data URI
    直传已被真机验证接受，音频按同机制直传；若服务端拒绝会以 400 形式反馈，
    届时可改用 audio_urls 填公网 URL。
    """
    file = Path(path)
    mime = _AUDIO_MIME.get(file.suffix.lower(), "application/octet-stream")
    try:
        data = file.read_bytes()
    except OSError as e:
        raise ValueError(f"{label} 读取失败：{path}（{e}）") from e
    if len(data) <= MAX_AUDIO_FILE_BYTES:
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    print(f"[Agnes AI] {label} {len(data) / 1048576:.1f}MB 超出 15MB，开始自动压缩（尽量保留音质）…")
    last_err = None
    for kbps in _AUDIO_COMPRESS_STEPS:
        try:
            out = _transcode_audio(path, kbps)
        except Exception as e:  # 编码器/环境问题：换档大概率同样失败，直接报错
            raise ValueError(
                f"{label} 自动压缩失败：{e}。请手动压缩音频至 15MB 内后重新上传。"
            ) from e
        if len(out) <= MAX_AUDIO_FILE_BYTES:
            print(f"[Agnes AI] {label} 压缩完成：AAC {kbps}k → {len(out) / 1048576:.1f}MB")
            return "data:audio/mp4;base64," + base64.b64encode(out).decode("ascii")
        last_err = f"AAC {kbps}k 仍为 {len(out) / 1048576:.1f}MB"
    raise ValueError(
        f"{label} 自动压缩到最小档仍超过 15MB 限制（{last_err}）。"
        "请手动剪辑音频后重新上传。"
    )


def _transcode_video(path: str, crf: int, scale: float) -> bytes:
    """用 PyAV 重编码视频：H.264 + 指定 CRF/缩放，丢弃音轨（API 以 require_audio=False 使用）。

    PyAV 是 ComfyUI 视频功能的必备依赖，环境内一定可用。
    """
    import av

    src = av.open(path)
    vstream = src.streams.video[0]
    fps = vstream.average_rate or vstream.guessed_rate or 24
    w, h = vstream.codec_context.width, vstream.codec_context.height
    new_w = max(2, int(w * scale) // 2 * 2)
    new_h = max(2, int(h * scale) // 2 * 2)

    buf = BytesIO()
    out = av.open(buf, "w", format="mp4")
    ostream = out.add_stream("libx264", rate=fps)
    ostream.width = new_w
    ostream.height = new_h
    ostream.pix_fmt = "yuv420p"
    ostream.options = {"crf": str(crf), "preset": "veryfast"}

    for frame in src.decode(video=0):
        frame = frame.reformat(width=new_w, height=new_h, format="yuv420p")
        for packet in ostream.encode(frame):
            out.mux(packet)
    for packet in ostream.encode():
        out.mux(packet)
    out.close()
    src.close()
    return buf.getvalue()


_VIDEO_COMPRESS_STEPS = [(22, 1.0), (26, 1.0), (30, 1.0), (30, 0.75), (32, 0.5), (35, 0.5)]


def video_file_to_data_uri(path: str, label: str = "参考视频") -> str:
    """本地视频文件 → data:video/*;base64 Data URI（随请求直传）。

    超过 50MB 时自动压缩（尽量保留画质）：CRF22（视觉接近无损）原分辨率起步，
    逐级降低 CRF / 分辨率，每档达标即返回；参考视频以 require_audio=False 交给
    API，压缩时丢弃音轨以把码率预算留给画面。
    """
    file = Path(path)
    mime = _VIDEO_MIME.get(file.suffix.lower(), "video/mp4")
    try:
        data = file.read_bytes()
    except OSError as e:
        raise ValueError(f"{label} 读取失败：{path}（{e}）") from e
    if len(data) <= MAX_VIDEO_FILE_BYTES:
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    print(f"[Agnes AI] {label} {len(data) / 1048576:.1f}MB 超出 50MB，开始自动压缩（尽量保留画质）…")
    last_err = None
    for crf, scale in _VIDEO_COMPRESS_STEPS:
        try:
            out = _transcode_video(path, crf, scale)
        except Exception as e:  # 编码器/环境问题：换档大概率同样失败，直接报错
            raise ValueError(
                f"{label} 自动压缩失败：{e}。请手动压缩视频至 50MB 内（2-12 秒）后重试。"
            ) from e
        if len(out) <= MAX_VIDEO_FILE_BYTES:
            side = "原分辨率" if scale == 1.0 else f"缩放至 {scale:g}x"
            print(f"[Agnes AI] {label} 压缩完成：CRF{crf} / {side} → {len(out) / 1048576:.1f}MB")
            return f"data:video/mp4;base64," + base64.b64encode(out).decode("ascii")
        last_err = f"CRF{crf}/{scale:g}x 仍为 {len(out) / 1048576:.1f}MB"
    raise ValueError(
        f"{label} 自动压缩到最小档仍超过 50MB 限制（{last_err}）。"
        "请手动剪辑视频（2-12 秒、≤50MB）后重新上传。"
    )


def video_from_file(path: str):
    """从本地文件构造 ComfyUI VIDEO 资产，兼容多版本 comfy_api 导入路径。

    候选顺序：模块顶层直接导出 → InputImpl 命名空间 → 旧版 shim/路径。
    """
    errors = []
    candidates = (
        ("comfy_api.latest", None, "VideoFromFile"),           # 顶层直接导出（新版）
        ("comfy_api.latest", "InputImpl", "VideoFromFile"),    # InputImpl.VideoFromFile
        ("comfy_api.input_impl", None, "VideoFromFile"),       # 向后兼容 shim
        ("comfy_api.video", None, "VideoFromFile"),            # 早期路径
    )
    for mod, ns, attr in candidates:
        try:
            module = __import__(mod, fromlist=[attr])
            container = getattr(module, ns) if ns is not None else module
            factory = getattr(container, attr)
            return factory(path)
        except (ImportError, AttributeError) as e:
            errors.append(f"{mod}{'.' + ns if ns else ''}.{attr}: {e}")
    raise ImportError(
        "无法导入 ComfyUI VIDEO 类型（VideoFromFile）。"
        "本节点需要较新的 ComfyUI（2025-07 之后，内置 comfy_api VIDEO 支持）。"
        "尝试过的路径：\n" + "\n".join(errors)
    )


def make_progress(total: int):
    """ComfyUI 进度条，非 ComfyUI 环境下降级为空操作。"""
    try:
        from comfy.utils import ProgressBar

        return ProgressBar(total)
    except Exception:
        class _Noop:
            def update(self, n=1):
                pass

            def update_absolute(self, value, total=None):
                pass

        return _Noop()
