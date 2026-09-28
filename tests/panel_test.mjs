// 素材面板前端逻辑的无浏览器确定性测试。
//
// 做法：把 web/agnes_media.js 的 import 换成 globalThis 上的替身后写入临时 .mjs 并动态导入，
// 用最小 DOM 替身驱动 onNodeCreated，验证「按模式置灰」的两条同步路径
// （widget.callback 与 onDrawForeground 兜底）以及上传前的跳过/超限提示。
//
// 运行：node tests/panel_test.mjs

import { readFile, writeFile, unlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCE = join(HERE, "..", "web", "agnes_media.js");

const FAILS = [];
const check = (name, cond, detail = "") => {
    console.log(`[${cond ? "PASS" : "FAIL"}] ${name}${!cond && detail !== "" ? `  -> ${detail}` : ""}`);
    if (!cond) FAILS.push(name);
};

// ---------------------------------------------------------------- DOM 替身
const created = [];
const makeElement = (tagName) => {
    const el = {
        tagName,
        children: [],
        style: {},
        textContent: "",
        listeners: {},
        append(...nodes) {
            el.children.push(...nodes);
        },
        appendChild(node) {
            el.children.push(node);
            return node;
        },
        replaceChildren(...nodes) {
            el.children = nodes;
        },
        addEventListener(type, fn) {
            (el.listeners[type] ||= []).push(fn);
        },
        dispatch(type, event) {
            for (const fn of el.listeners[type] || []) fn(event);
        },
        click() {
            el.onclick?.({ preventDefault() {}, stopPropagation() {} });
        },
    };
    created.push(el);
    return el;
};
globalThis.document = { createElement: makeElement };

// ---------------------------------------------------------------- 扩展注册替身
const registered = [];
globalThis.__agnesStubs = {
    app: {
        registerExtension(ext) {
            registered.push(ext);
        },
    },
    api: { apiURL: (path) => path },
};

const source = await readFile(SOURCE, "utf8");
const transformed = source
    .replace(/^import\s+\{\s*app\s*\}\s+from\s+"[^"]+";\s*$/m, "const app = globalThis.__agnesStubs.app;")
    .replace(/^import\s+\{\s*api\s*\}\s+from\s+"[^"]+";\s*$/m, "const api = globalThis.__agnesStubs.api;");
if (transformed === source) {
    console.error("无法改写 import：agnes_media.js 的 import 语句结构已变化");
    process.exit(1);
}

const TMP = join(tmpdir(), `agnes_panel_test_${process.pid}.mjs`);
await writeFile(TMP, transformed, "utf8");

const settle = async () => {
    for (let i = 0; i < 3; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
};

let fetchCalls = 0;
const uploadedNames = [];
globalThis.fetch = async (url, options) => {
    fetchCalls += 1;
    const file = options.body.get("image");
    uploadedNames.push(file.name);
    return { ok: true, status: 200, json: async () => ({ name: file.name, subfolder: "", type: "input" }) };
};

// FormData 只接受 Blob/File，size 用实例属性覆盖以避免真的分配大块内存
const fakeFile = (name, type, size) => {
    const file = new File([new Uint8Array(1)], name, { type });
    Object.defineProperty(file, "size", { value: size });
    return file;
};

const NodeType = class {};
const makeNode = () => {
    const mediaModeWidget = {
        name: "media_mode",
        type: "combo",
        value: { media_mode: "全能参考" },
        callback: null,
    };
    const mediaFilesWidget = { name: "media_files", type: "text", value: "[]", inputEl: { style: {} } };
    const domWidget = { name: "media_uploads", type: "div", element: null, computeSize: () => [100, 150] };
    return {
        widgets: [mediaModeWidget, mediaFilesWidget],
        size: [320, 400],
        widgetChangedCalls: [],
        domWidget,
        onWidgetChanged(...args) {
            this.widgetChangedCalls.push(args);
        },
        setDirtyCanvas() {},
        addDOMWidget(name, type, element) {
            domWidget.element = element;
            return domWidget;
        },
        onDrawForeground: undefined,
        onConfigure: undefined,
    };
};

try {
    await import(pathToFileURL(TMP).href);
    check("扩展已注册", registered.length === 1 && registered[0].name === "agnes.media.controls");

    const ext = registered[0];
    await ext.beforeRegisterNodeDef(NodeType, { name: "AgnesVideoGenerate" });

    const node = makeNode();
    NodeType.prototype.onNodeCreated.call(node);

    const [panel, toolbar] = created;
    const status = toolbar.children[0];
    const addButton = toolbar.children[1];
    const clearButton = toolbar.children[2];
    const fileInput = created[5];
    const list = created[6];
    const modeWidget = node.widgets[0];
    const mediaWidget = node.widgets[1];

    check("面板结构就位（panel/toolbar/按钮/列表）",
        panel.tagName === "div" && addButton.textContent === "＋ 添加素材"
        && clearButton.textContent === "清空" && list.tagName === "div" && fileInput.type === "file",
        `${panel.tagName}/${addButton.textContent}/${clearButton.textContent}/${list.tagName}/${fileInput.type}`);
    check("media_files widget 已隐藏", mediaWidget.type === "converted-widget" && mediaWidget.hidden === true);

    // ---- 1. 初始为「全能参考」→ 面板可用
    check("初始（全能参考）面板可用",
        addButton.disabled === false && clearButton.disabled === false
        && fileInput.disabled === false && panel.style.opacity === "1" && list.style.pointerEvents === "auto",
        `${addButton.disabled}/${clearButton.disabled}/${fileInput.disabled}/${panel.style.opacity}/${list.style.pointerEvents}`);
    check("初始状态行为默认提示", status.textContent.includes("点击「添加素材」"), status.textContent);

    // ---- 2. widget.callback 路径：切到文生视频 → 置灰
    modeWidget.callback({ media_mode: "文生视频" });
    check("callback 切文生视频 → 置灰禁用",
        addButton.disabled === true && clearButton.disabled === true
        && fileInput.disabled === true && panel.style.opacity === "0.55" && list.style.pointerEvents === "none",
        `${addButton.disabled}/${clearButton.disabled}/${fileInput.disabled}/${panel.style.opacity}/${list.style.pointerEvents}`);
    check("置灰时状态行给出原因", status.textContent === "素材仅「全能参考」模式使用，当前模式已停用", status.textContent);

    // ---- 3. callback 切回首尾帧（同为非参考模式）仍禁用
    modeWidget.callback({ media_mode: "图生视频 / 首尾帧" });
    check("callback 切首尾帧 → 仍禁用", addButton.disabled === true && panel.style.opacity === "0.55");

    // ---- 4. callback 切回全能参考 → 恢复可用
    modeWidget.callback({ media_mode: "全能参考" });
    check("callback 切回全能参考 → 恢复可用",
        addButton.disabled === false && fileInput.disabled === false && panel.style.opacity === "1"
        && status.textContent.includes("点击「添加素材」"),
        `${addButton.disabled}/${panel.style.opacity}/${status.textContent}`);

    // ---- 5. onDrawForeground 兜底路径：只改 widget.value（不触发 callback）
    modeWidget.value = { media_mode: "文生视频" };
    node.onDrawForeground();
    check("onDrawForeground 兜底：文生视频 → 置灰",
        addButton.disabled === true && panel.style.opacity === "0.55" && status.textContent.includes("已停用"),
        `${addButton.disabled}/${panel.style.opacity}`);

    // ---- 6. 兜底路径：模式未变化时不重复应用（状态保持禁用即可）
    const disabledBefore = addButton.disabled;
    node.onDrawForeground();
    check("模式未变时 onDrawForeground 幂等", addButton.disabled === disabledBefore);

    // ---- 7. 禁用态下拖入文件被忽略
    fetchCalls = 0;
    panel.dispatch("drop", {
        preventDefault() {},
        stopPropagation() {},
        dataTransfer: { files: [fakeFile("p1.png", "image/png", 10)] },
    });
    await settle();
    check("禁用态拖入被忽略（不发起上传）", fetchCalls === 0 && status.textContent.includes("已停用"), `${fetchCalls}/${status.textContent}`);

    // ---- 8. onConfigure：工作流加载后按保存的模式恢复状态
    modeWidget.value = { media_mode: "全能参考" };
    node.onDrawForeground(); // 让 modeNow 先回到全能参考
    modeWidget.value = { media_mode: "图生视频 / 首尾帧" };
    node.onConfigure();
    await settle();
    check("onConfigure 按保存的模式置灰", addButton.disabled === true && panel.style.opacity === "0.55",
        `${addButton.disabled}/${panel.style.opacity}`);

    // ---- 9. 上传前提示：全部为不支持的文件
    modeWidget.callback({ media_mode: "全能参考" });
    fetchCalls = 0;
    fileInput.files = [
        fakeFile("note.txt", "text/plain", 10),
        fakeFile("doc.pdf", "application/pdf", 10),
    ];
    fileInput.onchange();
    await settle();
    check("全部不支持 → 提示跳过且不上传",
        fetchCalls === 0 && status.textContent.includes("已跳过 2 个不支持的文件")
        && status.textContent.includes("note.txt") && status.textContent.includes("doc.pdf"),
        `${fetchCalls}/${status.textContent}`);

    // ---- 10. 上传前提示：支持文件 + 不支持文件 + 超限体积
    fetchCalls = 0;
    uploadedNames.length = 0;
    fileInput.files = [
        fakeFile("ok.png", "image/png", 1024),
        fakeFile("huge.png", "image/png", 20 * 1024 * 1024),
        fakeFile("note.txt", "text/plain", 10),
    ];
    fileInput.onchange();
    await settle();
    check("支持的文件被上传（逐文件一次请求）", fetchCalls === 2 && uploadedNames.join() === "ok.png,huge.png",
        `${fetchCalls}/${uploadedNames.join()}`);
    check("跳过项与超限体积均有提示",
        status.textContent.includes("已跳过 1 个不支持的文件") && status.textContent.includes("note.txt")
        && status.textContent.includes("1 个文件超过 API 体积上限") && status.textContent.includes("图片")
        && status.textContent.includes("自动压缩"),
        status.textContent);
    check("上传结果写入 media_files 清单",
        JSON.parse(mediaWidget.value).map((e) => e.name).join() === "ok.png [input],huge.png [input]",
        mediaWidget.value);
    check("清单变更通知 onWidgetChanged 传满 4 参（回归 reading 'options'）",
        node.widgetChangedCalls.length === 1 && node.widgetChangedCalls[0].length === 4,
        JSON.stringify(node.widgetChangedCalls.map((c) => c.length)));

    // ---- 11. 部分超数量上限：可容纳的先加，超出的提示跳过
    clearButton.click(); // 清空面板，让数量上限断言不受前序用例影响
    fetchCalls = 0;
    uploadedNames.length = 0;
    fileInput.files = Array.from({ length: 9 }, (_, i) =>
        fakeFile(`extra_${i}.png`, "image/png", 1024));
    fileInput.onchange();
    await settle();
    check("部分超限 → 只上传可容纳的 8 个",
        fetchCalls === 8 && uploadedNames[uploadedNames.length - 1] === "extra_7.png",
        `${fetchCalls}/${uploadedNames.join()}`);
    check("超出数量的文件给出跳过提示",
        status.textContent.includes("超出") && status.textContent.includes("extra_8.png"),
        status.textContent);

    // ---- 12. 已达数量上限：不再上传任何文件
    fetchCalls = 0;
    fileInput.files = [
        fakeFile("one.png", "image/png", 1024),
        fakeFile("two.png", "image/png", 1024),
    ];
    fileInput.onchange();
    await settle();
    check("已达上限 → 提示未添加且不上传",
        fetchCalls === 0 && status.textContent.includes("素材已达上限")
        && status.textContent.includes("one.png") && status.textContent.includes("two.png"),
        `${fetchCalls}/${status.textContent}`);
} finally {
    await unlink(TMP).catch(() => {});
}

console.log();
if (FAILS.length) {
    console.log(`❌ ${FAILS.length} 项失败：${FAILS.join(" | ")}`);
    process.exit(1);
}
console.log("✅ 素材面板前端测试全部通过");