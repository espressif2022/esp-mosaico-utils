import { expect, test } from "@playwright/test";

for (const force of [false, true]) {
  test(`project requests a device from its owner (force=${force})`, async ({ page }) => {
    const sessions = [
      { session_id: "session-a", project_path: "/projects/a", alive: true },
      { session_id: "session-b", project_path: "/projects/b", alive: true },
    ];
    let owner = "session-b";
    const actions: { action: string; body: Record<string, unknown> }[] = [];
    await page.route("**/v1/health", async (route) => {
      const response = await route.fetch();
      await route.fulfill({ json: { ...await response.json(), project_session: sessions[0] } });
    });
    await page.route("**/v1/project**", async (route) => {
      const request = route.request();
      if (request.method() === "POST") {
        const action = new URL(request.url()).pathname.split("/").pop()!;
        const body = request.postDataJSON();
        actions.push({ action, body });
        owner = action === "release" ? "" : "session-a";
        await route.fulfill({ json: { ok: true } });
        return;
      }
      await route.fulfill({ json: {
        session: sessions[0], sessions, closing: false, takeovers: [],
        lifecycle: { state: "running", idle_remaining_seconds: null, idle_timeout_seconds: 10,
          clients: [{ client_id: "client-run", kind: "run", command: "iris run", pid: 1234,
            connected_ns: 1_700_000_000_000_000_000, last_seen_ns: 1_700_000_000_000_000_000 }], keepalive: { clients: 1 } },
        endpoints: [{ endpoint: "usb:location=test", state: "connecting", ownership: owner ? {
          owner, owner_alive: true, device_id: "00112233445566778899aabbccddeeff", state: "owned", transfer_id: null,
        } : null }],
      } });
    });
    await page.goto("/");
    if (await page.getByLabel("开发口令").isVisible().catch(() => false)) {
      await page.getByRole("button", { name: "进入工作台" }).click();
    }
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await expect(page.getByText("项目会话与设备归属", { exact: true })).toBeVisible();
    await expect(page.getByLabel("网关使用者")).toContainText("iris run");
    await expect(page.getByLabel("网关使用者")).toContainText("PID 1234");
    await expect(page.getByRole("button", { name: "接管到本项目" })).toBeVisible();
    await expect(page.getByLabel("转让目标会话")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "转让设备" })).toHaveCount(0);
    expect(actions).toEqual([]);
    await page.screenshot({ path: `/tmp/esp-iris-takeover-${force}.png`, fullPage: true });
    await page.getByRole("button", { name: force ? "强制接管" : "接管到本项目", exact: true }).click();
    await expect(page.getByRole("button", { name: "释放设备" })).toBeVisible();
    expect(actions[0].action).toBe("takeovers");
    expect(actions[0].body.device_id).toBe("00112233445566778899aabbccddeeff");
    expect(actions[0].body.takeover_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(actions[0].body.force).toBe(force || undefined);
    await expect(page.getByRole("button", { name: "接管到本项目" })).toHaveCount(0);
    await page.getByRole("button", { name: "释放设备" }).click();
    await expect(page.getByRole("button", { name: "连接到本项目" })).toBeVisible();
    await page.getByRole("button", { name: "连接到本项目" }).click();
    await expect(page.getByRole("button", { name: "释放设备" })).toBeVisible();
    expect(actions[2]).toEqual({ action: "acquire", body: { endpoint: "usb:location=test" } });
  });
}

test("transferred device history stays readable without polling its former owner", async ({ page }) => {
  const history = { device_id: "old-device", suggested_alias: "Transferred device", connected: false, cached: true, state: "offline" };
  const current = { device_id: "current-device", suggested_alias: "Current device", connected: true, cached: false, state: "idle" };
  let forbiddenRequests = 0;
  await page.route("**/v1/devices**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/v1/devices") {
      await route.fulfill({ json: { demo: false, devices: [history, current] } });
    } else if (path === "/v1/devices/old-device") {
      forbiddenRequests += 1;
      await route.fulfill({ status: 409, json: { message: "device belongs to another project" } });
    } else if (path === "/v1/devices/current-device") {
      await route.fulfill({ json: { ...current, stale: false, mode: "develop" } });
    } else {
      await route.continue();
    }
  });
  await page.goto("/");
  if (await page.getByLabel("开发口令").isVisible().catch(() => false)) {
    await page.getByRole("button", { name: "进入工作台" }).click();
  }
  await expect(page.getByRole("heading", { name: "Current device", exact: true })).toBeVisible();
  await page.locator(".device-select").filter({ hasText: "Transferred device" }).click();
  await expect(page.getByRole("heading", { name: "Transferred device", exact: true })).toBeVisible();
  await page.waitForTimeout(2800);
  expect(forbiddenRequests).toBe(0);
  await expect(page.locator(".global-error")).toHaveCount(0);
});
