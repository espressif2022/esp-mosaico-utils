import { useLayoutEffect } from "react";

export type UiLanguage = "zh" | "en";

const translations: Record<string, string> = {
  "空闲": "Idle",
  "忙碌": "Busy",
  "连接中": "Connecting",
  "需恢复": "Needs recovery",
  "ROM 下载": "ROM download",
  "项目会话与设备归属": "Project session and device ownership",
  "项目": "Project",
  "操作完成": "Operation completed",
  "转让目标会话": "Receiving project session",
  "会话": "Session",
  "正在结束": "Draining",
  "转让目标": "Transfer target",
  "选择项目会话": "Select project session",
  "连接到本项目": "Connect to this project",
  "释放设备": "Release device",
  "转让设备": "Transfer device",
  "尝试接收转让": "Accept transfer",
  "未分配": "Unclaimed",
  "归属待核对": "Ownership requires reconciliation",
  "尚未发现设备。": "No devices discovered yet.",
  "发现设备不会自动连接。明确连接后，本会话会在设备重启时自动重连；转让后由目标会话接管。": "Discovery does not connect devices. Acquired devices reconnect after reboot; transferred devices belong to the receiving session.",
  "设备": "Devices",
  "记录": "Records",
  "设置": "Settings",
  "文件服务": "File service",
  "仅显示产品显式注册的逻辑卷": "Only product-registered logical volumes are shown",
  "仅显示产品显式注册的逻辑卷；写操作按设备串行并记录审计": "Only product-registered logical volumes are shown; writes are serialized per device and audited",
  "逻辑卷": "Logical volume",
  "刷新": "Refresh",
  "选择上传文件": "Choose a file to upload",
  "上传文件": "Upload file",
  "新建目录": "New directory",
  "文件路径": "File path",
  "名称": "Name",
  "类型": "Type",
  "大小": "Size",
  "修改时间": "Modified",
  "操作": "Action",
  "目录": "Directory",
  "文件": "File",
  "打开": "Open",
  "重命名": "Rename",
  "删除": "Delete",
  "目录为空": "Directory is empty",
  "正在读取目录…": "Reading directory…",
  "目录游标不是快照": "directory cursor is not a snapshot",
  "第": "From item ",
  "项起 ·": " · ",
  "上一页": "Previous",
  "下一页": "Next",
  "未选择设备": "No device selected",
  "从左侧选择一台设备以浏览文件": "Select a device on the left to browse files",
  "观察模式不访问设备文件": "Observe mode does not access device files",
  "切换到开发模式后可访问产品显式导出的逻辑卷": "Switch to Develop mode to access explicitly exported logical volumes",
  "设备当前离线": "Device is offline",
  "文件目录不会使用缓存结果": "File listings never use cached results",
  "设备未提供文件服务": "The device does not provide the file service",
  "产品需要在启动 ESP-Iris 前注册逻辑卷": "The product must register logical volumes before starting ESP-Iris",
  "查看设备操作及网关系统事件。": "Review device operations and gateway system events.",
  "设备操作": "Device operations",
  "系统事件": "System events",
  "选择一条操作查看详情": "Select an operation to view details",
  "搜索设备操作": "Search device operations",
  "搜索动作、参数或 ID": "Search actions, parameters, or IDs",
  "搜索系统事件": "Search system events",
  "搜索操作者、动作或详情": "Search actors, actions, or details",
  "事件详情": "Event details",
  "选择一条系统事件查看详情": "Select a system event to view details",
  "没有符合条件的系统事件": "No system events match the filters",
  "管理网关凭据、数据和开发者资源。": "Manage gateway credentials, data, and developer resources.",
  "本机访问": "Local access",
  "当前免认证": "Currently unauthenticated",
  "远程访问": "Remote access",
  "需要开发口令或具名 Agent Token": "Requires a developer password or named Agent Token",
  "当前模式": "Current mode",
  "在页面顶部切换": "switch from the page header",
  "网关信息": "Gateway information",
  "地址": "Address",
  "连接协议": "Connection protocol",
  "HTTP（未加密）": "HTTP (unencrypted)",
  "已启用": "Enabled",
  "未启用": "Disabled",
  "更改后当前浏览器会话将失效。": "Changing it signs out the current browser session.",
  "数据与开发者资源": "Data and developer resources",
  "导出证据包 ZIP": "Export evidence ZIP",
  "查看 API 文档": "View API documentation",
  "打开 OpenAPI JSON": "Open OpenAPI JSON",
  "Token 名称": "Token name",
  "Token 文件权限": "Token file permissions",
  "文件只读": "Read files",
  "文件读写": "Read and write files",
  "文件完全管理": "Full file management",
  "ESP-IRIS 网关的 OpenAPI 接口参考。": "OpenAPI reference for the ESP-IRIS gateway.",
  "写入设备的请求当前不可用。": "Requests that write to devices are currently unavailable.",
  "搜索设备": "Search devices",
  "清除搜索": "Clear search",
  "没有匹配的设备": "No matching devices",
  "移除": "Remove",
  "移除中…": "Removing…",
  "移除离线设备": "Remove offline device",
  "正常固件": "Normal firmware",
  "模式未知": "Mode unknown",
  "传输未知": "Transport unknown",
  "项目未知": "Project unknown",
  "设备信息": "Device information",
  "运行中": "Running",
  "设备能力": "Device capabilities",
  "读取系统信息": "Read system information",
  "系统信息已读取": "System information read",
  "调用 RPC…": "Call RPC…",
  "更多操作": "More actions",
  "OTA 更新": "OTA update",
  "系统更新包": "System update bundle",
  "上传签名 .irisfw 系统更新包": "Upload a signed .irisfw system update bundle",
  "上传无签名 .irisfw 系统更新包（无发布者认证）": "Upload an unsigned .irisfw system update bundle (publisher not authenticated)",
  "设备未提供只读 System Inventory": "Device does not provide read-only System Inventory",
  "Factory Recovery": "Factory Recovery",
  "重启设备": "Restart device",
  "当前操作": "Current operation",
  "镜像中": "Mirroring",
  "屏幕未启动": "Screen stopped",
  "点击“启动镜像”查看设备画面": "Select “Start mirror” to view the device screen",
  "最近操作": "Recent operations",
  "查看全部": "View all",
  "RPC 参数必须是有效 JSON": "RPC parameters must be valid JSON",
  "调用 RPC": "Call RPC",
  "方法": "Method",
  "JSON 参数": "JSON parameters",
  "关闭": "Close",
  "此操作会改变当前设备状态，请确认设备和操作无误。": "This changes the selected device. Confirm the device and action before continuing.",
  "显示口令": "Show password",
  "隐藏口令": "Hide password",
  "连接本地网关…": "Connecting to the local gateway…",
  "设备状态、Agent 操作、日志与恢复证据的唯一入口": "The single entry point for device state, Agent actions, logs, and recovery evidence",
  "两次口令不一致": "The passwords do not match",
  "开发者工作台": "Developer Workbench",
  "开发口令": "Developer password",
  "确认开发口令": "Confirm developer password",
  "连接中…": "Connecting…",
  "进入工作台": "Enter workbench",
  "初始化网关": "Initialize gateway",
  "已启用 HTTPS · 局域网开发环境": "HTTPS enabled · trusted LAN development environment",
  "HTTP · 仅限受信任局域网": "HTTP · trusted LAN only",
  "主导航": "Main navigation",
  "切换中…": "Switching…",
  "设备工作区": "Workspace",
  "设备概览": "Overview",
  "操作记录": "Operations",
  "系统设置": "Settings",
  "API 文档": "API Docs",
  "开发模式": "Develop mode",
  "观察模式": "Observe mode",
  "筛选设备": "Filter devices",
  "ID / 别名": "ID / alias",
  "尚未发现 ESP-Iris 设备": "No ESP-Iris device discovered",
  "恢复固件": "Recovery firmware",
  "未命名设备": "Unnamed device",
  "已连接": "Connected",
  "在线": "Online",
  "离线": "Offline",
  "等待设备": "Waiting for a device",
  "连接 ESP-Iris Normal 或启动 --demo": "Connect ESP-Iris Normal or start with --demo",
  "所有业务设备请求已停止；以下显示缓存状态和设备主动上报。": "All device requests are stopped; cached state and unsolicited device events remain visible.",
  "缓存状态 · 数据可能已过期": "Cached state · data may be stale",
  "运行时间": "Uptime",
  "最低": "minimum",
  "可用内部堆": "Free internal heap",
  "堆占用": "Heap usage",
  "生命周期": "Lifecycle",
  "日志丢失": "Dropped log bytes",
  "时钟误差": "Clock uncertainty",
  "设备控制": "Device controls",
  "原始 RPC": "Raw RPC",
  "逐行 Console": "Line Console",
  "Console 命令": "Console command",
  "发送中…": "Sending…",
  "输入 help 查看可用命令。": "Enter help to list available commands.",
  "命令串行执行；输出通过设备日志通道返回。请勿输入口令或其他敏感信息。": "Commands run serially and output returns through device logs. Do not enter passwords or other secrets.",
  "取消 Job": "Cancel job",
  "重启": "Restart",
  "设备操作时间线": "Device operation timeline",
  "已送达": "Delivered",
  "尚无设备操作": "No device operations yet",
  "设备画面": "Device screen",
  "未启动": "Stopped",
  "PNG 截图": "PNG screenshot",
  "截图": "Screenshot",
  "PNG 截图已完成，实时镜像保持运行": "PNG screenshot captured; live mirroring is still running",
  "停止镜像": "Stop mirror",
  "启动镜像": "Start mirror",
  "正在启动镜像…": "Starting mirror…",
  "正在停止镜像…": "Stopping mirror…",
  "正在读取并转换截图，请稍候…": "Reading and converting screenshot…",
  "处理中…": "Working…",
  "交互输入": "Interactive input",
  "设备实时画面": "Live device screen",
  "启动镜像后显示设备画面 · 默认 5 FPS": "Start mirroring to view the device screen · default 5 FPS",
  "Agent 输入": "Agent input",
  "音频": "Audio",
  "录音（最长 60s）": "Record (up to 60s)",
  "上传 WAV/PCM": "Upload WAV/PCM",
  "下载 WAV": "Download WAV",
  "实时流默认不落盘 · 上传/保存上限 16 MiB": "Live streams are not persisted by default · upload/save limit 16 MiB",
  "调用 RPC Catalog": "Call RPC Catalog",
  "发送原始 RPC": "Send raw RPC",
  "确认重启设备": "Confirm device restart",
  "进入 Factory Recovery": "Enter Factory Recovery",
  "执行 OTA 更新": "Run OTA update",
  "执行系统更新": "Run system update",
  "执行位置": "Execution location",
  "Factory Recovery（默认）": "Factory Recovery (default)",
  "当前应用": "Current application",
  "完成后校验": "Post-update validation",
  "ELF SHA-256（默认）": "ELF SHA-256 (default)",
  "项目版本号（兼容）": "Project version (compatibility)",
  "BIN 文件": "BIN file",
  "ELF 文件": "ELF file",
  "MAP 文件": "MAP file",
  "选择 .elf 文件": "Choose an .elf file",
  "选择 .map 文件": "Choose a .map file",
  "IRISFW 文件": "IRISFW file",
  "选择系统更新包": "Choose a system update bundle",
  "选择 .irisfw 文件": "Choose an .irisfw file",
  "Gateway 会按当前信任策略校验并保存原始包，再保全 Core Dump、进入 Recovery、提交更新，最后校验新固件与实际 Flash 清单。": "The Gateway validates and archives the bundle according to the active trust policy, preserves the core dump, enters Recovery, commits the update, then validates the new firmware and actual Flash inventory.",
  "取消设备 Job": "Cancel device job",
  "原始载荷": "Raw payload",
  "ESP32-S31 应用镜像": "ESP32-S31 application image",
  "选择 .bin 文件": "Choose a .bin file",
  "设备会在 250 ms 后重启。操作会记录到设备时间线，重连结果由网关继续观察。": "The device will restart after 250 ms. The action is recorded in the timeline and the gateway follows reconnection.",
  "设备将选择固定 factory recovery 并执行 planned restart。该能力必须由固件显式提供。": "The device selects the fixed factory recovery image and performs a planned restart. Firmware must explicitly provide this capability.",
  "需要开发者确认": "Developer confirmation required",
  "此操作会改变设备状态；Agent Token 调用不需要额外批准。": "This changes device state; Agent Token calls require no additional approval.",
  "取消": "Cancel",
  "执行中…": "Running…",
  "确认执行": "Confirm",
  "设备日志": "Device logs",
  "日志标签": "Log tag",
  "搜索日志": "Search logs",
  "搜索原始日志": "Search raw logs",
  "继续": "Resume",
  "暂停": "Pause",
  "跟随": "Follow",
  "下载": "Download",
  "历史缺口：部分日志已超出保留范围": "History gap: some logs are outside the retention window",
  "等待设备日志…": "Waiting for device logs…",
  "当前连接、固件与操作状态；V1 不提供批量写入。": "Current connections, firmware, and operation state; V1 provides no bulk writes.",
  "已发现设备": "Discovered devices",
  "NORMAL / RECOVERY 统一历史": "Unified NORMAL / RECOVERY history",
  "进行中操作": "Active operations",
  "跨设备并行，单设备串行": "Parallel across devices, serialized per device",
  "失败 / 未知": "Failed / unknown",
  "需要开发者检查": "Developer review required",
  "设备清单": "Device inventory",
  "连接": "Connection",
  "设备 / ID": "Device / ID",
  "固件模式": "Firmware mode",
  "项目 / 版本": "Project / version",
  "传输": "Transport",
  "最近行为": "Latest action",
  "打开工作区 →": "Open workspace →",
  "能力矩阵": "Capability matrix",
  "设备声明": "Device-declared",
  "最后刷新": "Last refreshed",
  "只记录真实设备侧行为；代码修改、构建和 PC Shell 不进入此时间线。": "Only real device-side actions are recorded; code edits, builds, and PC shell activity are excluded.",
  "全部设备": "All devices",
  "全部操作者": "All actors",
  "全部状态": "All statuses",
  "搜索动作、参数、ID": "Search action, parameters, or ID",
  "时间": "Time",
  "操作者": "Actor",
  "动作": "Action",
  "状态": "Status",
  "耗时": "Duration",
  "队列": "Queue",
  "运行": "Running",
  "条": "items",
  "没有符合筛选条件的操作": "No operations match the filters",
  "操作详情": "Operation details",
  "创建时间": "Created",
  "设备 ID": "Device ID",
  "开始": "Started",
  "结束": "Finished",
  "已净化参数": "Sanitized parameters",
  "结果": "Result",
  "选择一条操作查看阶段、参数和结果": "Select an operation to inspect phases, parameters, and result",
  "全局模式、开发口令、Agent Token、TLS、存储与 RPC Catalog。": "Global mode, developer password, Agent Tokens, TLS, storage, and RPC Catalog.",
  "本机认证状态、远程访问凭据、运行模式、TLS、存储与 RPC Catalog。": "Local authentication status, remote-access credentials, runtime mode, TLS, storage, and RPC Catalog.",
  "访问认证": "Access authentication",
  "本机回环访问": "Local loopback access",
  "需要开发口令或 Agent Token": "Developer password or Agent Token required",
  "默认免认证": "Unauthenticated by default",
  "局域网与其他设备": "LAN and other devices",
  "始终需要开发口令或具名 Agent Token": "Always require a developer password or named Agent Token",
  "仅依据实际 TCP 对端地址判断，不信任 X-Forwarded-For。": "Determined only from the actual TCP peer; X-Forwarded-For is not trusted.",
  "运行模式": "Runtime mode",
  "开放全部设备接口；Agent 默认模式": "All device APIs enabled; the Agent default",
  "停止所有业务设备请求，仅显示缓存和主动上报": "Stop all device requests; show only cache and unsolicited events",
  "远程 Agent Token": "Remote Agent Tokens",
  "Token 名称，例如 codex-bench-a": "Token name, for example codex-bench-a",
  "创建 Token": "Create token",
  "仅显示一次": "Shown once",
  "复制": "Copy",
  "创建于": "created",
  "最近使用": "last used",
  "已撤销": "Revoked",
  "有效": "Active",
  "撤销": "Revoke",
  "共享认证": "Shared authentication",
  "远程开发口令": "Remote developer password",
  "新的开发口令": "New developer password",
  "更改口令": "Change password",
  "观察页面也必须通过此口令认证。V1 不创建多开发者账号。": "Observation pages also require this password. V1 has no per-developer accounts.",
  "局域网和其他非回环访问必须通过此口令认证。V1 不创建多开发者账号。": "LAN and other non-loopback access must authenticate with this password. V1 has no per-developer accounts.",
  "TLS 与网络": "TLS and network",
  "当前来源": "Current origin",
  "协议": "Protocol",
  "HTTP（默认，未加密）": "HTTP (default, unencrypted)",
  "局域网访问": "LAN access",
  "其他设备仍需认证": "other devices still require authentication",
  "使用 --listen 0.0.0.0；其他设备仍需认证": "Use --listen 0.0.0.0; other devices still require authentication",
  "可选 HTTPS": "Optional HTTPS",
  "使用 --tls，并在客户端信任证书或指纹": "Use --tls and trust the certificate or fingerprint on the client",
  "，并在客户端信任证书或指纹": " and trust the certificate or fingerprint on the client",
  "使用": "Use",
  "并在 Windows 信任证书或指纹": "and trust the certificate or fingerprint on Windows",
  "使用 --listen 0.0.0.0 并在 Windows 信任证书或指纹": "Use --listen 0.0.0.0 and trust the certificate or fingerprint on Windows",
  "存储与导出": "Storage and export",
  "结构化记录 / 工件": "Structured records / artifacts",
  "默认永久保留": "Retained indefinitely by default",
  "原始设备日志": "Raw device logs",
  "7 天或 1 GiB": "7 days or 1 GiB",
  "实时媒体": "Live media",
  "默认不落盘": "Not persisted by default",
  "导出完整证据包 ZIP": "Export complete evidence ZIP",
  "Demo 与发现": "Demo and discovery",
  "Demo 模式": "Demo mode",
  "USB 自动发现": "USB auto-discovery",
  "默认传输": "Default transport",
  "系统审计": "System audit",
  "非设备行为": "Non-device activity",
  "网关设备接口的可执行参考；CLI 与 Agent 使用相同的 /v1 契约。": "Executable reference for gateway device APIs; the CLI and Agent share the same /v1 contract.",
  "可以查看文档；设备业务请求会返回 423，不会到达设备。": "Documentation remains available; device requests return 423 and never reach the device.",
  "请求": "Request",
  "发送": "Send",
  "响应": "Response",
  "在这里执行已认证的 /v1 请求": "Run an authenticated /v1 request here",
  "在这里执行 /v1 请求": "Run a /v1 request here",
  "API 文档与试验台": "API Documentation and Console",
  "RPC 已完成": "RPC completed",
  "原始 RPC 已完成": "Raw RPC completed",
  "重启请求已发送": "Restart request sent",
  "设备已进入 factory recovery": "Device entered factory recovery",
  "Job 取消请求已发送": "Job cancellation requested",
  "OTA 验收闭环已完成": "OTA acceptance loop completed",
  "系统更新": "System update",
  "系统更新已受理": "System update accepted",
  "请在时间线查看闭环校验": "follow closed-loop validation in the timeline",
  "截图失败": "Screenshot failed",
  "音频上传失败": "Audio upload failed",
  "导出失败": "Export failed",
  "证据包已导出": "Evidence bundle exported",
};

const originalText = new WeakMap<Text, string>();
const originalAttributes = new WeakMap<Element, Map<string, string>>();
const attributes = ["aria-label", "placeholder", "alt", "title"];

function translate(value: string): string {
  const leading = value.match(/^\s*/)?.[0] ?? "";
  const trailing = value.match(/\s*$/)?.[0] ?? "";
  const core = value.slice(leading.length, value.length - trailing.length);
  if (!core) return value;
  const exact = translations[core];
  if (exact) return `${leading}${exact}${trailing}`;
  let translated = core;
  for (const [source, target] of Object.entries(translations).sort((a, b) => b[0].length - a[0].length)) {
    if (translated.includes(source)) translated = translated.replaceAll(source, target);
  }
  return `${leading}${translated}${trailing}`;
}

function excluded(node: Node): boolean {
  const parent = node instanceof Element ? node : node.parentElement;
  return Boolean(parent?.closest("[data-no-translate], .log-body, pre, code"));
}

function applyText(node: Text, language: UiLanguage) {
  if (excluded(node)) return;
  const current = node.nodeValue ?? "";
  if (language === "zh") {
    const original = originalText.get(node);
    if (original !== undefined && current !== original) node.nodeValue = original;
    originalText.delete(node);
    return;
  }
  if (!/[\u3400-\u9fff]/u.test(current)) return;
  originalText.set(node, current);
  const next = translate(current);
  if (next !== current) node.nodeValue = next;
}

function applyAttributes(element: Element, language: UiLanguage) {
  if (excluded(element)) return;
  let originals = originalAttributes.get(element);
  for (const name of attributes) {
    const current = element.getAttribute(name);
    if (current === null) continue;
    if (language === "zh") {
      const original = originals?.get(name);
      if (original !== undefined && current !== original) element.setAttribute(name, original);
      originals?.delete(name);
    } else if (/[\u3400-\u9fff]/u.test(current)) {
      originals ??= new Map<string, string>();
      originals.set(name, current);
      const next = translate(current);
      if (next !== current) element.setAttribute(name, next);
    }
  }
  if (originals?.size) originalAttributes.set(element, originals);
}

function applyElement(element: Element, language: UiLanguage) {
  if (excluded(element)) return;
  applyAttributes(element, language);
  for (const child of element.querySelectorAll("[aria-label], [placeholder], [alt], [title]")) applyAttributes(child, language);
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) applyText(node as Text, language);
}

export function useUiTranslation(language: UiLanguage) {
  useLayoutEffect(() => {
    applyElement(document.body, language);
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        if (record.type === "characterData") applyText(record.target as Text, language);
        if (record.type === "attributes") applyElement(record.target as Element, language);
        for (const node of record.addedNodes) {
          if (node instanceof Text) applyText(node, language);
          else if (node instanceof Element) applyElement(node, language);
        }
      }
    });
    observer.observe(document.body, { attributes: true, childList: true, characterData: true, subtree: true });
    return () => observer.disconnect();
  }, [language]);
}
