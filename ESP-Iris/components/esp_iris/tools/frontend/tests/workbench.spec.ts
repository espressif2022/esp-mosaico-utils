import { expect, test } from "@playwright/test";

test("desktop workbench keeps the device workflow focused", async ({ page }) => {
  await page.route("**/v1/devices", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    for (const device of body.devices) {
      device.boot_id = Number("12238782771570883527");
      device.boot_id_text = "12238782771570883527";
    }
    await route.fulfill({ response, json: body });
  });
  const errors: string[] = [];
  let systemUpdateUploaded = false;
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.route("**/v1/health", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, system_update_trust_configured: true } });
  });
  await page.route("**/v1/devices/*/system-update", async (route) => {
    systemUpdateUploaded = route.request().postDataBuffer()?.equals(Buffer.from("signed-irisfw-test")) ?? false;
    await route.fulfill({
      status: 202,
      json: { operation: { operation_id: "system-update-ui-test" }, accepted: true },
    });
  });
  await page.goto("/");
  await expect(page).toHaveTitle("ESP-Iris 开发者工作台");
  await expect.poll(() => page.evaluate(() => getComputedStyle(document.documentElement).colorScheme)).toBe("light");

  const password = page.getByLabel("开发口令");
  if (await password.isVisible().catch(() => false)) {
    await expect(password).toHaveValue("espressif");
    await expect(password).toHaveAttribute("type", "password");
    await page.getByRole("button", { name: "显示口令" }).click();
    await expect(password).toHaveAttribute("type", "text");
    await page.getByRole("button", { name: "隐藏口令" }).click();
    await expect(password).toHaveAttribute("type", "password");
    await page.getByRole("button", { name: "进入工作台" }).click();
  }

  await expect(page.getByRole("navigation", { name: "主导航" }).getByRole("button")).toHaveCount(4);
  await expect(page.getByRole("button", { name: "设备", exact: true })).toBeVisible();
  await expect(page.getByText("Camera Bench").first()).toBeVisible();
  // Camera Bench intentionally disconnects on the demo's periodic schedule.
  // Exercise the online workflow against the stable device explicitly rather
  // than whichever recent device the inventory initially selects.
  await page.getByRole("button", { name: /Mosaico Alpha demo-a1b2c3d/ }).click();
  await expect(page.getByRole("heading", { name: "Mosaico Alpha" })).toBeVisible();
  await page.locator(".device-details summary").click();
  await expect(page.locator(".device-details dd").filter({ hasText: /^12238782771570883527$/ })).toBeVisible();
  await page.locator(".device-details summary").click();
  const selectedDeviceName = await page.locator(".device-row.selected strong").innerText();
  const observeMode = page.getByRole("button", { name: "观察模式", exact: true });
  if (await observeMode.isVisible().catch(() => false)) {
    await observeMode.click();
    await expect(page.getByRole("button", { name: "开发模式", exact: true })).toBeVisible();
  }

  const originalCount = await page.locator(".device-row").count();
  await page.getByLabel("搜索设备").fill("not-a-real-device");
  await expect(page.getByText("没有匹配的设备")).toBeVisible();
  await page.getByLabel("清除搜索").click();
  await expect(page.locator(".device-row")).toHaveCount(originalCount);

  await page.getByText("更多操作", { exact: true }).click();
  await expect(page.getByRole("button", { name: "Factory Recovery" })).toBeVisible();
  await expect(page.getByRole("button", { name: "读取系统信息" })).toBeVisible();
  await page.getByRole("button", { name: "调用 RPC…" }).click();
  await expect(page.getByRole("heading", { name: "调用 RPC" })).toBeVisible();
  await expect(page.getByLabel("方法")).toHaveValue("system.info");
  await page.getByLabel("关闭").click();

  await page.getByRole("button", { name: "OTA 更新", exact: true }).click();
  await expect(page.getByRole("heading", { name: "执行 OTA 更新" })).toBeVisible();
  const validationMode = page.getByLabel("完成后校验");
  await expect(validationMode).toHaveValue("elf_sha256");
  await validationMode.selectOption("version");
  await expect(validationMode).toHaveValue("version");
  await validationMode.selectOption("elf_sha256");
  await page.screenshot({ path: process.env.ESP_IRIS_OTA_SCREENSHOT || "/tmp/esp-iris-ota-validation.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(validationMode).toBeVisible();
  await page.screenshot({ path: process.env.ESP_IRIS_OTA_MOBILE_SCREENSHOT || "/tmp/esp-iris-ota-validation-mobile.png" });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByLabel("关闭").click();

  await page.getByText("更多操作", { exact: true }).click();
  await page.getByRole("button", { name: "系统更新包", exact: true }).click();
  await expect(page.getByRole("heading", { name: "执行系统更新" })).toBeVisible();
  const confirmSystemUpdate = page.getByRole("button", { name: "确认执行" });
  await expect(confirmSystemUpdate).toBeDisabled();
  await page.getByLabel("选择系统更新包").setInputFiles({
    name: "release.irisfw",
    mimeType: "application/vnd.esp-iris.system-update+zip",
    buffer: Buffer.from("signed-irisfw-test"),
  });
  await expect(page.getByText("release.irisfw", { exact: true })).toBeVisible();
  await expect(confirmSystemUpdate).toBeEnabled();
  await page.screenshot({ path: process.env.ESP_IRIS_SYSTEM_UPDATE_SCREENSHOT || "/tmp/esp-iris-system-update.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(confirmSystemUpdate).toBeVisible();
  await page.screenshot({ path: process.env.ESP_IRIS_SYSTEM_UPDATE_MOBILE_SCREENSHOT || "/tmp/esp-iris-system-update-mobile.png" });
  await page.setViewportSize({ width: 1440, height: 900 });
  await confirmSystemUpdate.click();
  await expect(page.getByText("系统更新已受理", { exact: false })).toBeVisible();
  expect(systemUpdateUploaded).toBe(true);

  await page.getByRole("button", { name: "记录", exact: true }).click();
  await expect(page.getByRole("tab", { name: "设备操作" })).toBeVisible();
  await page.getByRole("tab", { name: "系统事件" }).click();
  await expect(page.getByText("gateway.started").first()).toBeVisible();

  await page.getByRole("button", { name: "文件", exact: true }).click();
  await expect(page.getByRole("heading", { name: selectedDeviceName })).toBeVisible();
  await expect(page.getByRole("link", { name: "下载" }).first()).toBeVisible();
  await page.getByLabel("选择上传文件").setInputFiles({
    name: "browser.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("uploaded through the Files workbench\n"),
  });
  await expect(page.getByText("已上传 browser.txt", { exact: true })).toBeVisible();

  page.once("dialog", (dialog) => void dialog.accept("browser-dir"));
  await page.getByRole("button", { name: "新建目录", exact: true }).click();
  await expect(page.getByText("已创建目录 browser-dir", { exact: true })).toBeVisible();

  const browserFile = page.getByRole("row").filter({ hasText: "browser.txt" });
  page.once("dialog", (dialog) => void dialog.accept("renamed.txt"));
  await browserFile.getByRole("button", { name: "重命名", exact: true }).click();
  await expect(page.getByText("已将 browser.txt 重命名为 renamed.txt", { exact: true })).toBeVisible();

  const renamedFile = page.getByRole("row").filter({ hasText: "renamed.txt" });
  page.once("dialog", (dialog) => void dialog.accept());
  await renamedFile.getByRole("button", { name: "删除", exact: true }).click();
  await expect(page.getByText("已删除 renamed.txt", { exact: true })).toBeVisible();

  const browserDirectory = page.getByRole("row").filter({ hasText: "browser-dir" });
  page.once("dialog", (dialog) => void dialog.accept());
  await browserDirectory.getByRole("button", { name: "删除", exact: true }).click();
  await expect(page.getByText("已删除 browser-dir", { exact: true })).toBeVisible();
  await page.getByText("certs", { exact: true }).click();
  await expect(page.getByText("device.pem", { exact: true })).toBeVisible();
  await page.screenshot({ path: process.env.ESP_IRIS_FILES_SCREENSHOT || "/tmp/esp-iris-files-page.png", fullPage: true });

  await page.getByRole("button", { name: "设置", exact: true }).click();
  await expect(page.getByText("Agent Token", { exact: true })).toBeVisible();
  await expect(page.getByText("网关信息", { exact: true })).toBeVisible();
  await expect(page.getByText("RPC Catalog", { exact: true })).toHaveCount(0);
  await expect(page.getByText("系统审计", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "API 文档", exact: true }).click();
  await expect(page.getByRole("heading", { name: "API 文档" })).toBeVisible();
  await expect(page.getByText("/v1/health", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "发送", exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "设备", exact: true }).click();
  await page.getByRole("button", { name: "开发模式", exact: true }).click();
  await expect(page.getByText("所有业务设备请求已停止", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "读取系统信息" })).toBeDisabled();

  await page.getByRole("button", { name: "EN", exact: true }).click();
  await expect(page.getByRole("button", { name: "Devices", exact: true })).toBeVisible();
  const untranslated = new Set<string>();
  for (const destination of ["Files", "Records", "Settings", "API Docs", "Devices"]) {
    await page.getByRole("button", { name: destination, exact: true }).click();
    const values = await page.locator("body").evaluate((body) => {
      const found = new Set<string>();
      const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const parent = node.parentElement;
        const value = node.nodeValue?.trim() || "";
        if (/[\u3400-\u9fff]/u.test(value) && !parent?.closest(".language-button, .log-body, pre, code, [data-no-translate]")) found.add(value);
      }
      for (const element of body.querySelectorAll("[aria-label], [placeholder], [alt], [title]")) {
        if (element.closest(".language-button, .log-body, pre, code, [data-no-translate]")) continue;
        for (const name of ["aria-label", "placeholder", "alt", "title"]) {
          const value = element.getAttribute(name) || "";
          if (/[\u3400-\u9fff]/u.test(value)) found.add(`${name}=${value}`);
        }
      }
      return [...found];
    });
    for (const value of values) untranslated.add(`${destination}: ${value}`);
  }
  expect([...untranslated]).toEqual([]);
  await page.screenshot({ path: process.env.ESP_IRIS_SCREENSHOT || "/tmp/esp-iris-workbench.png", fullPage: true });
  expect(errors).toEqual([]);
});

test("an offline device can be removed from inventory", async ({ page }) => {
  let removed = false;
  await page.route("**/v1/devices**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/v1/devices" && request.method() === "GET") {
      await route.fulfill({
        json: {
          demo: false,
          devices: removed ? [] : [{
            device_id: "offline-device",
            suggested_alias: "Retired Bench",
            connected: false,
            state: "offline",
            cached: true,
            firmware_mode: "normal",
            app_version: "3.5.0",
          }],
        },
      });
      return;
    }
    if (path === "/v1/devices/offline-device" && request.method() === "DELETE") {
      removed = true;
      await route.fulfill({ json: { device_id: "offline-device", removed: true, history_preserved: true } });
      return;
    }
    if (path === "/v1/devices/offline-device" && request.method() === "GET") {
      await route.fulfill({
        json: {
          device_id: "offline-device",
          suggested_alias: "Retired Bench",
          connected: false,
          state: "offline",
          cached: true,
          stale: true,
          mode: "observe",
          firmware_mode: "normal",
        },
      });
      return;
    }
    await route.continue();
  });
  page.on("dialog", async (dialog) => {
    expect(dialog.message()).toContain("操作记录和日志会继续保留");
    await dialog.accept();
  });

  await page.goto("/");
  const password = page.getByLabel("开发口令");
  if (await password.isVisible().catch(() => false)) {
    await page.getByRole("button", { name: "进入工作台" }).click();
  }
  await expect(page.getByRole("heading", { name: "Retired Bench" })).toBeVisible();
  await page.screenshot({ path: process.env.ESP_IRIS_OFFLINE_SCREENSHOT || "/tmp/esp-iris-offline-device.png", fullPage: true });
  await page.getByRole("button", { name: "移除离线设备 Retired Bench" }).click();
  await expect(page.getByRole("heading", { name: "Retired Bench" })).toHaveCount(0);
  await expect(page.getByText("尚未发现 ESP-Iris 设备")).toBeVisible();
  expect(removed).toBe(true);
});

test("operation details show calendar dates across midnight", async ({ page }) => {
  await page.route("**/v1/operations", async (route) => {
    await route.fulfill({
      json: {
        operations: [{
          operation_id: "cross-day-operation",
          device_id: "demo-a1b2c3d4",
          actor_type: "developer",
          actor_name: "Developer",
          action: "system.info",
          params: {},
          status: "succeeded",
          result: { ok: true },
          created_ns: Date.UTC(2026, 7, 24, 15, 59, 50) * 1e6,
          started_ns: Date.UTC(2026, 7, 24, 15, 59, 55) * 1e6,
          finished_ns: Date.UTC(2026, 7, 24, 16, 0, 5) * 1e6,
          queue_position: 0,
        }],
      },
    });
  });

  await page.goto("/");
  const password = page.getByLabel("开发口令");
  if (await password.isVisible().catch(() => false)) {
    await page.getByRole("button", { name: "进入工作台" }).click();
  }
  await page.getByRole("button", { name: "记录", exact: true }).click();
  await page.locator(".records-table tbody tr").first().click();
  const detail = page.locator(".record-detail");
  await expect(detail).toContainText("2026-08-24 23:59:50");
  await expect(detail).toContainText("2026-08-24 23:59:55");
  await expect(detail).toContainText("2026-08-25 00:00:05");
  await page.screenshot({ path: process.env.ESP_IRIS_RECORD_TIME_SCREENSHOT || "/tmp/esp-iris-record-time.png", fullPage: true });
});
