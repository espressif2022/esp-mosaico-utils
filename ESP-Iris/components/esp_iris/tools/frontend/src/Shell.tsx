import { useDeferredValue, useMemo, useState } from "react";
import { deviceStateLabel } from "./deviceState";
import type { Device } from "./types";

export type Page = "devices" | "files" | "records" | "settings" | "docs";

type HeaderProps = {
  page: Page;
  setPage: (value: Page) => void;
  mode: "develop" | "observe";
  transitioning: boolean;
  setMode: (value: "develop" | "observe") => Promise<void>;
  demo: boolean;
  language: "zh" | "en";
  setLanguage: (value: "zh" | "en") => void;
};

const labels = {
  zh: { devices: "设备", files: "文件", records: "记录", settings: "设置", docs: "API 文档", develop: "开发模式", observe: "观察模式" },
  en: { devices: "Devices", files: "Files", records: "Records", settings: "Settings", docs: "API Docs", develop: "Develop", observe: "Observe" },
};

export function Header({ page, setPage, mode, transitioning, setMode, demo, language, setLanguage }: HeaderProps) {
  const t = labels[language];
  return (
    <header className="app-header">
      <div className="brand-lockup"><span className="brand-mark small">IR</span><span>ESP-IRIS</span></div>
      <nav aria-label="主导航">
        {(["devices", "files", "records", "settings"] as Page[]).map((item) => (
          <button key={item} className={page === item ? "active" : ""} onClick={() => setPage(item)}>{t[item]}</button>
        ))}
      </nav>
      <div className="header-actions">
        {demo && <span className="demo-badge">DEMO</span>}
        <button className={`mode-switch ${mode}`} disabled={transitioning} onClick={() => setMode(mode === "develop" ? "observe" : "develop") }>
          <span className="status-dot" /> {transitioning ? "切换中…" : t[mode]}
        </button>
        <button className={`utility-button ${page === "docs" ? "active" : ""}`} onClick={() => setPage("docs")}>{t.docs}</button>
        <button className="language-button" onClick={() => setLanguage(language === "zh" ? "en" : "zh")}>{language === "zh" ? "EN" : "中"}</button>
      </div>
    </header>
  );
}

type RailProps = {
  devices: Device[];
  selectedId: string;
  onSelect: (deviceId: string) => void;
  onRemove: (deviceId: string) => Promise<void>;
  language: "zh" | "en";
};

export function DeviceRail({ devices, selectedId, onSelect, onRemove, language }: RailProps) {
  const [query, setQuery] = useState("");
  const [removingId, setRemovingId] = useState("");
  const [removeError, setRemoveError] = useState("");
  const deferredQuery = useDeferredValue(query.trim().toLowerCase());
  const filtered = useMemo(() => {
    if (!deferredQuery) return devices;
    return devices.filter((device) => [
      device.alias,
      device.suggested_alias,
      device.device_id,
      device.project_name,
      device.transport_name,
      device.endpoint,
    ].some((value) => value?.toLowerCase().includes(deferredQuery)));
  }, [deferredQuery, devices]);

  async function remove(device: Device) {
    const name = device.alias || device.suggested_alias || device.device_id;
    const message = language === "zh"
      ? `移除离线设备“${name}”？\n\n设备重新连接后会再次出现，操作记录和日志会继续保留。`
      : `Remove offline device “${name}”?\n\nIt will reappear when it reconnects. Operations and logs will be preserved.`;
    if (!window.confirm(message)) return;
    setRemovingId(device.device_id);
    setRemoveError("");
    try {
      await onRemove(device.device_id);
    } catch (reason) {
      setRemoveError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setRemovingId("");
    }
  }

  return (
    <aside className="device-rail">
      <div className="rail-heading"><span>设备</span><small>{devices.filter((device) => device.connected).length}/{devices.length}</small></div>
      <div className="rail-search"><span aria-hidden="true">⌕</span><input aria-label="搜索设备" placeholder="搜索设备" value={query} onChange={(event) => setQuery(event.target.value)} />{query && <button aria-label="清除搜索" onClick={() => setQuery("")}>×</button>}</div>
      <div className="device-list">
        {filtered.map((device) => (
          <div key={device.device_id} className={`device-row ${selectedId === device.device_id ? "selected" : ""}`}>
            <button className="device-select" onClick={() => onSelect(device.device_id)}>
              <span className={`device-icon ${device.firmware_mode || "unknown"}`}>{device.firmware_mode === "recovery" ? "R" : device.firmware_mode === "normal" ? "N" : "?"}</span>
              <span className="device-copy">
                <strong>{device.alias || device.suggested_alias || device.device_id.slice(0, 12)}</strong>
                <small>{device.device_id.slice(0, 12)}</small>
                {device.hardware_mac && <small>{device.hardware_mac}</small>}
                <span><i className={`status-dot ${device.state}`} />{deviceStateLabel(device.state, language)} · {firmwareModeLabel(device.firmware_mode)}</span>
                <small>{device.transport_name || device.endpoint || "传输未知"}</small>
              </span>
              <em>{device.app_version || "—"}</em>
            </button>
            {device.state === "offline" && <button className="device-remove" disabled={removingId === device.device_id} aria-label={`移除离线设备 ${device.alias || device.suggested_alias || device.device_id}`} onClick={() => void remove(device)}>{removingId === device.device_id ? "移除中…" : "移除"}</button>}
          </div>
        ))}
        {removeError && <p className="rail-error">{removeError}</p>}
        {!devices.length && <p className="empty-state">尚未发现 ESP-Iris 设备</p>}
        {devices.length > 0 && !filtered.length && <div className="empty-state"><p>没有匹配的设备</p><button onClick={() => setQuery("")}>清除搜索</button></div>}
      </div>
    </aside>
  );
}

export function firmwareModeLabel(mode?: Device["firmware_mode"]) {
  if (mode === "rom") return "ROM 下载";
  if (mode === "normal") return "正常固件";
  if (mode === "recovery") return "恢复固件";
  return "模式未知";
}
