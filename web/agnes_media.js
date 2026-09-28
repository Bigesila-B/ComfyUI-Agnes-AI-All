// Agnes AI 素材上传面板：为 AgnesVideoGenerate 提供「点击选择 / 拖入」的素材上传区。
// 素材清单以 JSON 存入节点的 media_files 字符串 widget：
//   [{"type": "image|audio|video", "name": "文件名 [input]"}, ...]
// 上传走 ComfyUI 内置 /upload/image 端点（通用上传，type=input）。

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const parseMedia = (value) => {
    try {
        const entries = JSON.parse(value || "[]");
        return Array.isArray(entries) ? entries : [];
    } catch {
        return [];
    }
};

const kindOf = (file) => {
    if (file.type?.startsWith("image/")) return "image";
    if (file.type?.startsWith("audio/")) return "audio";
    if (file.type?.startsWith("video/")) return "video";
    const ext = (file.name || "").split(".").pop().toLowerCase();
    if (["png", "jpg", "jpeg", "webp", "gif", "bmp"].includes(ext)) return "image";
    if (["mp3", "wav", "m4a", "aac", "ogg", "flac", "opus"].includes(ext)) return "audio";
    if (["mp4", "webm", "mov", "mkv", "avi", "m4v"].includes(ext)) return "video";
    return null;
};

const ICONS = { image: "🖼️", audio: "🎵", video: "🎬" };
const MAX_PER_KIND = { image: 8, audio: 3, video: 1 };
// 后端会自动压缩超限文件，这里只做上传前提示，不拦截
const MAX_BYTES = { image: 15 * 1024 * 1024, audio: 15 * 1024 * 1024, video: 50 * 1024 * 1024 };
const KIND_LABELS = { image: "图片", audio: "音频", video: "视频" };
const REFERENCE_MODE = "全能参考";
const MODE_DISABLED_HINT = "素材仅「全能参考」模式使用，当前模式已停用";
const LIMIT_HINT = `上限 图${MAX_PER_KIND.image}·音${MAX_PER_KIND.audio}·视频${MAX_PER_KIND.video}`;

const nameList = (names, limit = 3) =>
    names.slice(0, limit).join("、") + (names.length > limit ? ` 等 ${names.length} 个` : "");

// media_mode 是 DynamicCombo：widget.value 在保存与回调时都是 { media_mode: "..." } 对象，
// 仅个别前端版本给字符串，统一归一化成选项文案再比较
const modeFromValue = (value) => {
    if (typeof value === "string") return value;
    if (value && typeof value === "object") return value.media_mode ?? "";
    return "";
};

const currentMode = (node) =>
    modeFromValue(node.widgets?.find((w) => w.name === "media_mode")?.value);

const previewUrl = (name) => {
    const clean = name.replace(/ \[(input|output|temp)\]$/, "");
    const slash = clean.lastIndexOf("/");
    const params = new URLSearchParams({
        filename: slash >= 0 ? clean.slice(slash + 1) : clean,
        subfolder: slash >= 0 ? clean.slice(0, slash) : "",
        type: "input",
    });
    return api.apiURL(`/view?${params}`);
};

const hideWidget = (widget) => {
    if (!widget || widget.type === "converted-widget") return;
    widget.origType = widget.type;
    widget.origComputeSize = widget.computeSize;
    widget.type = "converted-widget";
    widget.computeSize = () => [0, -4];
    widget.serializeValue = async () => widget.value;
    widget.hidden = true;
    if (widget.inputEl?.style) widget.inputEl.style.display = "none";
};

const panelHeight = (entries) => (entries.length ? 190 : 150);

const resizePanel = (widget, width, entries) => {
    if (!widget) return;
    widget.computeSize = () => [Math.max(1, width - 20), panelHeight(entries?.length ?? 0)];
    const element = widget.element ?? widget.domWidget?.element ?? widget.el;
    if (element?.style) {
        element.style.width = "100%";
        element.style.maxWidth = "100%";
        element.style.minWidth = "0";
    }
};

app.registerExtension({
    name: "agnes.media.controls",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "AgnesVideoGenerate") return;

        const originalCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            originalCreated?.apply(this, arguments);
            // 闭包内统一用 node：DynamicCombo 的 widget.callback 以 widget 作 this，
            // 回调里写 this.xxx 会取到 widget 而非节点
            const node = this;

            const mediaWidget = this.widgets?.find((w) => w.name === "media_files");
            if (!mediaWidget) {
                console.warn("[Agnes AI] 未找到 media_files widget，素材面板不可用");
                return;
            }
            hideWidget(mediaWidget);

            const mediaModeWidget = this.widgets?.find((w) => w.name === "media_mode");
            // 找不到模式控件时不启用置灰逻辑：宁可面板保持可用，也不要卡死在禁用态
            const modeGating = !!mediaModeWidget;
            let modeNow = currentMode(this);
            const modeEnabled = () => !modeGating || modeNow === REFERENCE_MODE;

            const panel = document.createElement("div");
            panel.style.cssText = [
                "display:flex", "flex-direction:column", "gap:8px", "width:100%",
                "max-width:100%", "min-width:0", "padding:5px 10px", "border:1px solid #555",
                "border-radius:8px", "background:#202020", "color:#ddd",
                "box-sizing:border-box", "overflow:hidden", "position:relative", "z-index:2",
                "pointer-events:auto",
            ].join(";");

            const toolbar = document.createElement("div");
            toolbar.style.cssText = [
                "display:flex", "align-items:center", "gap:6px", "width:100%",
                "max-width:100%", "min-width:0", "box-sizing:border-box",
                "position:relative", "z-index:3", "pointer-events:auto",
            ].join(";");

            const status = document.createElement("span");
            status.style.cssText = "flex:1;min-width:0;font-size:12px;color:#aaa;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;pointer-events:none;";

            const addButton = document.createElement("button");
            addButton.type = "button";
            addButton.textContent = "＋ 添加素材";
            const clearButton = document.createElement("button");
            clearButton.type = "button";
            clearButton.textContent = "清空";
            for (const button of [addButton, clearButton]) {
                button.style.cssText = [
                    "display:inline-flex", "align-items:center", "justify-content:center",
                    "height:28px", "box-sizing:border-box", "padding:4px 10px",
                    "border:1px solid #666", "border-radius:5px", "background:#333", "color:#eee",
                    "cursor:pointer", "position:relative", "z-index:4", "pointer-events:auto",
                    "font-size:12px",
                ].join(";");
            }

            const fileInput = document.createElement("input");
            fileInput.type = "file";
            fileInput.accept = "image/*,audio/*,video/*";
            fileInput.multiple = true;
            fileInput.style.display = "none";

            const list = document.createElement("div");
            list.style.cssText = [
                "display:flex", "flex-direction:column", "gap:5px", "width:100%",
                "max-width:100%", "min-width:0", "max-height:120px", "overflow-y:auto",
                "border:1px dashed #666", "border-radius:7px", "padding:6px",
                "box-sizing:border-box", "transition:border-color .15s,background .15s",
            ].join(";");

            toolbar.append(status, addButton, clearButton);
            panel.append(toolbar, list, fileInput);
            const domWidget = this.addDOMWidget("media_uploads", "div", panel, { serialize: false });
            resizePanel(domWidget, this.size?.[0] ?? 300, []);

            const renderMedia = () => {
                const entries = parseMedia(mediaWidget.value);
                const counts = { image: 0, audio: 0, video: 0 };
                for (const entry of entries) counts[entry.type] = (counts[entry.type] || 0) + 1;
                status.textContent = entries.length
                    ? `已添加 ${entries.length} 个素材（图 ${counts.image} / 音 ${counts.audio} / 视频 ${counts.video}），${LIMIT_HINT}`
                    : "点击「添加素材」选择文件，或把图片/音频/视频拖到虚线框内";
                list.replaceChildren();
                entries.forEach((entry, index) => {
                    const row = document.createElement("div");
                    row.style.cssText = "display:flex;align-items:center;gap:8px;min-width:0;";
                    const icon = document.createElement("span");
                    icon.textContent = ICONS[entry.type] || "📄";
                    icon.style.cssText = "flex:none;font-size:16px;";
                    const label = document.createElement("span");
                    label.textContent = entry.name.replace(/ \[(input|output|temp)\]$/, "");
                    label.style.cssText = "flex:1;min-width:0;font-size:12px;color:#ccc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";
                    row.append(icon, label);
                    if (entry.type === "image") {
                        const thumb = document.createElement("img");
                        thumb.src = previewUrl(entry.name);
                        thumb.title = "图片预览";
                        thumb.style.cssText = "width:40px;height:28px;object-fit:cover;border-radius:4px;border:1px solid #555;flex:none;";
                        row.append(thumb);
                    } else if (entry.type === "video") {
                        // 视频素材：显示首帧画面作为预览
                        const thumb = document.createElement("video");
                        thumb.src = previewUrl(entry.name) + "#t=0.1";
                        thumb.muted = true;
                        thumb.preload = "metadata";
                        thumb.title = "视频首帧预览";
                        thumb.style.cssText = "width:40px;height:28px;object-fit:cover;border-radius:4px;border:1px solid #555;flex:none;pointer-events:none;";
                        row.append(thumb);
                    }
                    const remove = document.createElement("button");
                    remove.type = "button";
                    remove.textContent = "✕";
                    remove.title = "移除该素材";
                    remove.style.cssText = "flex:none;width:24px;height:24px;border:1px solid #666;border-radius:4px;background:#333;color:#eee;cursor:pointer;font-size:11px;";
                    remove.onclick = (event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        setMedia(entries.filter((_, i) => i !== index));
                    };
                    row.append(remove);
                    list.append(row);
                });
                if (!modeEnabled()) status.textContent = MODE_DISABLED_HINT;
            };

            const setMedia = (entries) => {
                const previous = mediaWidget.value;
                mediaWidget.value = JSON.stringify(entries);
                mediaWidget.callback?.(mediaWidget.value);
                // 第 4 参必须传 widget 本身：前端 GraphView 会给 onWidgetChanged 包一层钩子，
                // 钩子内直接读 widget.options（无保护），少传即抛 reading 'options'
                this.onWidgetChanged?.(mediaWidget.name, mediaWidget.value, previous, mediaWidget);
                renderMedia();
                resizePanel(domWidget, this.size?.[0] ?? 300, entries);
                this.setDirtyCanvas?.(true, true);
            };

            const processFiles = async (fileList) => {
                const accepted = [];
                const unsupported = [];
                for (const file of Array.from(fileList || [])) {
                    const kind = kindOf(file);
                    if (kind) accepted.push({ file, kind });
                    else unsupported.push(file.name || "(未命名文件)");
                }
                if (!accepted.length) {
                    status.textContent = unsupported.length
                        ? `已跳过 ${unsupported.length} 个不支持的文件（仅支持图片/音频/视频）：${nameList(unsupported)}`
                        : "没有检测到可用的图片/音频/视频文件";
                    return;
                }
                const entries = parseMedia(mediaWidget.value);
                const counts = { image: 0, audio: 0, video: 0 };
                for (const entry of entries) counts[entry.type] = (counts[entry.type] || 0) + 1;
                const keep = [];
                const overLimit = [];
                for (const item of accepted) {
                    counts[item.kind] += 1;
                    if (counts[item.kind] > MAX_PER_KIND[item.kind]) overLimit.push(item.file.name || "(未命名文件)");
                    else keep.push(item);
                }
                if (!keep.length) {
                    status.textContent = `素材已达上限（${LIMIT_HINT}），未添加：${nameList(overLimit)}`;
                    return;
                }
                // 超限文件仍会上传，后端按类型自动压缩；这里只提前说明
                const oversized = keep.filter(({ file, kind }) => (file.size || 0) > MAX_BYTES[kind]);
                addButton.disabled = true;
                try {
                    for (let index = 0; index < keep.length; index += 1) {
                        const { file, kind } = keep[index];
                        status.textContent = `正在上传素材 ${index + 1}/${keep.length}：${file.name}`;
                        const form = new FormData();
                        form.append("image", file, file.name);
                        form.append("type", "input");
                        form.append("overwrite", "false");
                        // 用原生 fetch 直连上传端点：不同前端版本的 api.fetchApi 对
                        // options 的内部处理不一致（曾报 reading 'options'），直连最稳
                        const response = await fetch(api.apiURL("/upload/image"), {
                            method: "POST",
                            body: form,
                        });
                        const uploaded = await response.json();
                        if (!response.ok || !uploaded.name) {
                            throw new Error(uploaded.error || `上传失败：HTTP ${response.status}`);
                        }
                        const path = uploaded.subfolder ? `${uploaded.subfolder}/${uploaded.name}` : uploaded.name;
                        entries.push({ type: kind, name: `${path} [${uploaded.type || "input"}]` });
                    }
                    setMedia(entries);
                    // 汇总本次跳过项与超限说明，覆盖 renderMedia 写回的计数文案
                    const notes = [];
                    if (unsupported.length) notes.push(`已跳过 ${unsupported.length} 个不支持的文件：${nameList(unsupported)}`);
                    if (overLimit.length) notes.push(`超出${LIMIT_HINT}未添加：${nameList(overLimit)}`);
                    if (oversized.length) {
                        notes.push(
                            `${oversized.length} 个文件超过 API 体积上限（${oversized
                                .map(({ kind }) => KIND_LABELS[kind])
                                .filter((label, i, arr) => arr.indexOf(label) === i)
                                .join("/")}），发送时将自动压缩`
                        );
                    }
                    if (notes.length) status.textContent = notes.join("；");
                } catch (error) {
                    status.textContent = `素材上传失败：${error.message}`;
                } finally {
                    addButton.disabled = false;
                }
            };

            fileInput.onchange = () => {
                processFiles(fileInput.files);
                fileInput.value = "";
            };
            for (const element of [addButton, clearButton]) {
                element.onpointerdown = (event) => event.stopPropagation();
            }
            addButton.onclick = (event) => {
                event.preventDefault();
                event.stopPropagation();
                fileInput.click();
            };
            clearButton.onclick = (event) => {
                event.preventDefault();
                event.stopPropagation();
                setMedia([]);
            };
            for (const type of ["mousedown", "mouseup", "click", "auxclick", "dblclick"]) {
                panel.addEventListener(type, (event) => event.stopPropagation());
            }
            panel.addEventListener("dragover", (event) => {
                event.preventDefault();
                event.stopPropagation();
                if (!modeEnabled()) return;
                list.style.borderColor = "#8ab4f8";
                list.style.background = "rgba(138,180,248,.12)";
                status.textContent = "松开鼠标即可添加素材";
            });
            panel.addEventListener("dragleave", (event) => {
                event.preventDefault();
                list.style.borderColor = "#666";
                list.style.background = "transparent";
                renderMedia();
            });
            panel.addEventListener("drop", (event) => {
                event.preventDefault();
                event.stopPropagation();
                if (!modeEnabled()) return;
                list.style.borderColor = "#666";
                list.style.background = "transparent";
                processFiles(event.dataTransfer?.files);
            });

            const applyModeState = () => {
                const enabled = modeEnabled();
                hideWidget(this.widgets?.find((w) => w.name === "media_files"));
                addButton.disabled = !enabled;
                clearButton.disabled = !enabled;
                fileInput.disabled = !enabled;
                list.style.pointerEvents = enabled ? "auto" : "none";
                panel.style.opacity = enabled ? "1" : "0.55";
                renderMedia();
                this.setDirtyCanvas?.(true, true);
            };

            // 实测本前端版本：切换 DynamicCombo 只会触发 widget.callback
            // （节点级 onWidgetChanged 不触发）；回调传入整个动态值对象，且 this 指向 widget
            if (mediaModeWidget) {
                const originalModeCallback = mediaModeWidget.callback;
                mediaModeWidget.callback = function (value, ...rest) {
                    const result = originalModeCallback?.apply(this, [value, ...rest]);
                    modeNow = modeFromValue(value) || currentMode(node);
                    applyModeState();
                    return result;
                };
            }

            applyModeState();

            // 兜底：个别前端版本可能不触发 widget.callback，绘制时比对模式，仅在变化时同步
            const originalDrawForeground = node.onDrawForeground;
            node.onDrawForeground = function () {
                originalDrawForeground?.apply(this, arguments);
                const latest = currentMode(node);
                if (latest !== modeNow) {
                    modeNow = latest;
                    applyModeState();
                }
            };

            const originalConfigure = this.onConfigure;
            this.onConfigure = function () {
                originalConfigure?.apply(this, arguments);
                queueMicrotask(() => {
                    modeNow = currentMode(this);
                    applyModeState();
                });
            };
        };
    },
});
