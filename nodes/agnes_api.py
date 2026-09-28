"""Agnes AI API 客户端（同步 requests 实现），为 Agnes 节点提供底层调用。

文档索引：https://wiki.agnes-ai.cn/llms.txt
（节点与文案统一用 Agnes；模型 ID 与服务域名为 API 协议值，保持服务端原样小写）

- 图像：POST {base}/images/generations（OpenAI 风格；response_format 必须放 extra_body 内）
- 视频：POST {base}/videos 创建异步任务，轮询 GET {origin}/agnesapi?video_id=...&model_name=...
  （注意轮询端点挂在域名根，不在 /v1 下；完成后视频 mp4 地址在响应顶层 url 字段）
"""

import base64
import json
import re
import time

import requests

from .utils import assert_public_http_url, download_file_to_bytes

BASE_URL_CN = "https://api.agnes-ai.cn/v1"
BASE_URL_GLOBAL = "https://apihub.agnes-ai.com/v1"

IMAGE_MODELS = ["agnes-image-2.5-flash", "agnes-image-2.1-flash", "agnes-image-2.0-flash"]
VIDEO_MODELS = ["agnes-video-2.5", "agnes-video-2.5-flash"]

VIDEO_TASK_QUEUED = "queued"
VIDEO_TASK_IN_PROGRESS = "in_progress"
VIDEO_TASK_COMPLETED = "completed"
VIDEO_TASK_FAILED = "failed"
# 实际观测到但官方文档未列出的等待类状态
VIDEO_TASK_PENDING = "pending"

# 无需打印提示、继续等待即可的状态
_QUIET_TASK_STATUSES = {VIDEO_TASK_QUEUED, VIDEO_TASK_IN_PROGRESS, VIDEO_TASK_PENDING}

_RETRY_STATUS = {429, 500, 502, 503, 504}

_STATUS_DESC = {
    400: "请求参数无效（检查 size/ratio/mode 与媒体字段是否匹配，response_format 需放 extra_body）",
    401: "API Key 无效或未授权，请检查 api_key",
    403: "无权限访问该资源",
    404: "任务或资源不存在",
    429: "请求过于频繁（触发限频）",
    500: "Agnes AI 服务端错误",
    503: "Agnes AI 服务繁忙，请稍后重试",
}


class AgnesApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AgnesClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL_CN):
        self.base_url = (base_url or BASE_URL_CN).strip().rstrip("/")
        assert_public_http_url(self.base_url, field="base_url")
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    @property
    def _origin(self) -> str:
        """域名根（去掉 /v1），用于 /agnesapi 轮询端点。"""
        return re.sub(r"/v\d+$", "", self.base_url)

    def _request(self, method: str, url: str, *, json_body=None, params=None,
                 timeout=(10, 300), retries: int = 3) -> requests.Response:
        last = None
        for attempt in range(retries):
            try:
                resp = requests.request(
                    method, url, headers=self.headers, json=json_body, params=params, timeout=timeout
                )
                if resp.status_code in _RETRY_STATUS:
                    last = AgnesApiError(
                        f"{_STATUS_DESC.get(resp.status_code, f'HTTP {resp.status_code}')}：{resp.text[:500]}",
                        status=resp.status_code,
                    )
                elif resp.status_code >= 400:
                    raise AgnesApiError(
                        f"Agnes API 错误 {_STATUS_DESC.get(resp.status_code, f'HTTP {resp.status_code}')}"
                        f"：{resp.text[:500]}",
                        status=resp.status_code,
                    )
                else:
                    return resp
            except requests.RequestException as e:
                last = e
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 8))
        raise AgnesApiError(f"请求失败（已尝试 {retries} 次）：{last}")

    # ------------------------------------------------------------------ 图像
    def generate_image(self, *, model: str, prompt: str, size: str, ratio: str,
                       image_uris: list[str] | None = None) -> bytes:
        """文生图/图生图/多图合成，统一走 b64_json 返回（省一次下载）。"""
        extra_body = {"response_format": "b64_json"}
        if image_uris:
            extra_body["image"] = list(image_uris)
        body = {"model": model, "prompt": prompt, "size": size, "extra_body": extra_body}
        if ratio:
            body["ratio"] = ratio

        resp = self._request("POST", f"{self.base_url}/images/generations",
                             json_body=body, timeout=(10, 360))
        try:
            data = resp.json()
        except ValueError as e:
            raise AgnesApiError(f"图像接口返回非 JSON：{resp.text[:300]}") from e
        items = data.get("data") or []
        if not items:
            raise AgnesApiError(f"图像接口未返回数据：{json.dumps(data, ensure_ascii=False)[:500]}")
        item = items[0]
        if item.get("b64_json"):
            try:
                return base64.b64decode(item["b64_json"])
            except (ValueError, TypeError) as e:
                raise AgnesApiError(f"返回的 b64_json 解码失败：{e}") from e
        if item.get("url"):
            return download_file_to_bytes(item["url"])
        raise AgnesApiError("图像接口返回项中既无 b64_json 也无 url")

    # ------------------------------------------------------------------ 视频
    def create_video(self, payload: dict) -> dict:
        # 创建任务不做自动重试：若服务端已建任务而本次响应超时/被网关中断，
        # 重试会重复建任务、按秒重复计费
        resp = self._request("POST", f"{self.base_url}/videos", json_body=payload,
                             timeout=(10, 120), retries=1)
        try:
            data = resp.json()
        except ValueError as e:
            raise AgnesApiError(f"视频任务创建返回非 JSON：{resp.text[:300]}") from e
        task_id = data.get("video_id") or data.get("task_id") or data.get("id")
        if not task_id:
            raise AgnesApiError(f"创建视频任务失败，未返回任务 ID：{json.dumps(data, ensure_ascii=False)[:500]}")
        return data

    def poll_video(self, task_id: str, model: str, *, interval: float = 3.0,
                   timeout: float = 1200, progress_cb=None) -> dict:
        """轮询直到 completed/failed。

        查询接口限频较严：遇到 429/5xx/网络抖动/200+非 JSON 响应时自动拉长间隔
        继续等待（最长 +30s），不终止任务，直到成功查询或整体超时。
        查询基础间隔随已等待时间自动放缓（30s 内 3s → 90s 内 5s → 之后 8s），
        降低长任务期间触发限频的概率。任务刚创建时查询端可能有短暂 404 延迟，
        前几次查询予以容忍。

        进度回调：响应带 progress 时按累计百分比上报；排队阶段服务端不返回该字段，
        改按已等待时长估算 0–30% 上报，避免进度条长时间停在 0。
        """
        url = f"{self._origin}/agnesapi"
        params = {"video_id": task_id, "model_name": model}
        start = time.time()
        backoff = 0.0
        queries = 0
        last_unknown = None
        reported = -1
        while True:
            elapsed = time.time() - start
            if elapsed > timeout:
                raise AgnesApiError(
                    f"轮询超时（{timeout:.0f}s），任务 {task_id} 未完成。"
                    "视频任务可能仍在排队，可加大 poll_timeout 后重试。"
                )
            queries += 1
            try:
                resp = self._request("GET", url, params=params, timeout=(10, 60), retries=2)
            except AgnesApiError as e:
                transient = e.status in _RETRY_STATUS or e.status is None
                early_404 = e.status == 404 and queries <= 3
                if transient or early_404:
                    backoff = min(max(backoff * 2, 4.0), 30.0)
                    print(
                        f"[Agnes AI] 查询限频/服务繁忙/网络抖动，{(interval + backoff):.0f}s "
                        f"后继续等待任务 {task_id}…（{e}）"
                    )
                    time.sleep(interval + backoff)
                    continue
                raise
            backoff = 0.0
            try:
                data = resp.json()
            except ValueError:
                # 网关/WAF 可能返回 200 + HTML 挑战页：按瞬时故障退避后重查
                backoff = min(max(backoff * 2, 4.0), 30.0)
                print(f"[Agnes AI] 轮询返回非 JSON 响应，退避 {(interval + backoff):.0f}s 后重查任务 {task_id}…")
                time.sleep(interval + backoff)
                continue
            status = data.get("status")
            if progress_cb is not None:
                raw = data.get("progress")
                if isinstance(raw, (int, float)):
                    # progress 为累计百分比（0-100），回调方应使用绝对值语义更新进度条
                    value = int(raw)
                else:
                    # 排队阶段无 progress 字段：按已等待时长估算 0–30%，让进度条始终在动
                    value = min(30, int(elapsed / timeout * 30)) if timeout > 0 else 0
                # 只上报递增值：真实 progress 起点可能低于排队期的估算值，回退看着像卡住
                if value > reported:
                    reported = value
                    progress_cb(value)
            if status == VIDEO_TASK_COMPLETED:
                if not self.extract_result_url(data):
                    raise AgnesApiError(
                        f"任务完成但未返回视频 url：{json.dumps(data, ensure_ascii=False)[:500]}"
                    )
                return data
            if status == VIDEO_TASK_FAILED:
                raise AgnesApiError(
                    f"视频生成失败：{data.get('error') or json.dumps(data, ensure_ascii=False)[:500]}"
                )
            if status not in _QUIET_TASK_STATUSES:
                # 未知状态只提示一次，直到状态回到已知集合
                if status != last_unknown:
                    print(f"[Agnes AI] 任务状态 {status!r}（未知），继续等待…")
                    last_unknown = status
            else:
                last_unknown = None
            # 基础间隔随等待时长放缓，减少长任务期间的限频触发
            time.sleep(interval if elapsed < 30 else (5.0 if elapsed < 90 else 8.0))

    @staticmethod
    def extract_result_url(data: dict) -> str | None:
        """从完成响应提取视频地址：新版在顶层 url，旧版在 metadata.url。"""
        meta = data.get("metadata")
        url = data.get("url")
        if not url and isinstance(meta, dict):
            url = meta.get("url")
        return url or None
