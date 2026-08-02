import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type APIRequestContext, type Page, type Route } from "@playwright/test";

const apiBase = "http://127.0.0.1:18000";
const currentDir = path.dirname(fileURLToPath(import.meta.url));
const dataDir = path.resolve(currentDir, "..", "..", "..", ".tmp", "playwright-data");

type ScriptStep = {
  purpose: string;
  agent_id: string;
  match_contains: string;
  delay_ms?: number;
  output_json: Record<string, unknown>;
};

async function cloneDefaultProfile(request: APIRequestContext): Promise<Record<string, unknown>> {
  const response = await request.post(`${apiBase}/api/v1/profile-templates/default-natural-chat-v1/clone`);
  expect(response.ok()).toBeTruthy();
  const payload = (await response.json()) as { profile: Record<string, unknown> };
  return payload.profile;
}

async function createRuntimeConversation(
  request: APIRequestContext,
  title: string,
  script: ScriptStep[],
): Promise<string> {
  const profile = await cloneDefaultProfile(request);
  const modules = profile.modules as Record<string, Record<string, unknown>>;
  modules.model_adapter = {
    module_id: "model.scripted",
    config: { script },
  };
  profile.metadata = {
    ...(profile.metadata as Record<string, unknown> | undefined),
    phase: 4,
    agent_runtime_enabled: true,
  };

  const created = await request.post(`${apiBase}/api/v1/conversations`, {
    data: { title, draft_profile: profile },
  });
  expect(created.ok()).toBeTruthy();
  const payload = (await created.json()) as { id: string };
  const conversationId = payload.id;

  expect((await request.post(`${apiBase}/api/v1/conversations/${conversationId}/validate`)).ok()).toBeTruthy();
  expect((await request.post(`${apiBase}/api/v1/conversations/${conversationId}/start`)).ok()).toBeTruthy();
  return conversationId;
}

async function endRunningConversations(request: APIRequestContext): Promise<void> {
  const response = await request.get(`${apiBase}/api/v1/conversations`);
  expect(response.ok()).toBeTruthy();
  const payload = (await response.json()) as { items: Array<{ id: string; status: string }> };
  for (const item of payload.items) {
    if (item.status === "running") {
      expect((await request.post(`${apiBase}/api/v1/conversations/${item.id}/end`)).ok()).toBeTruthy();
    }
  }
}

async function findConversationIdByTitle(request: APIRequestContext, title: string): Promise<string> {
  await expect
    .poll(
      async () => {
        const response = await request.get(`${apiBase}/api/v1/conversations`);
        const payload = (await response.json()) as { items: Array<{ id: string; title: string }> };
        return payload.items.find((item) => item.title === title)?.id ?? null;
      },
      { timeout: 10_000 },
    )
    .not.toBeNull();

  const response = await request.get(`${apiBase}/api/v1/conversations`);
  const payload = (await response.json()) as { items: Array<{ id: string; title: string }> };
  const item = payload.items.find((entry) => entry.title === title);
  expect(item).toBeDefined();
  return item!.id;
}

async function markHistoryAsUnknownAdapter(conversationId: string): Promise<void> {
  const manifestPath = path.join(dataDir, "experiments", conversationId, "manifest.json");
  const manifest = JSON.parse(await fs.readFile(manifestPath, "utf-8")) as Record<string, unknown>;
  const draftProfile = manifest.draft_profile as Record<string, unknown>;
  const draftModules = draftProfile.modules as Record<string, Record<string, unknown>>;
  draftModules.model_adapter = {
    ...(draftModules.model_adapter ?? {}),
    module_id: "model.legacy-unknown",
  };
  if (manifest.locked_profile && typeof manifest.locked_profile === "object") {
    const lockedProfile = manifest.locked_profile as Record<string, unknown>;
    const lockedModules = lockedProfile.modules as Record<string, Record<string, unknown>>;
    lockedModules.model_adapter = {
      ...(lockedModules.model_adapter ?? {}),
      module_id: "model.legacy-unknown",
    };
  }
  await fs.writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf-8");
}

async function selectConversation(page: Page, title: string): Promise<void> {
  await page.getByRole("button", { name: new RegExp(title) }).first().click();
}

async function installSocketTracker(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const NativeWebSocket = window.WebSocket;
    const sockets: WebSocket[] = [];
    class TrackedWebSocket extends NativeWebSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        sockets.push(this);
      }
    }
    Object.setPrototypeOf(TrackedWebSocket, NativeWebSocket);
    Object.defineProperty(window, "__palChatSockets", {
      value: sockets,
      configurable: false,
      enumerable: false,
      writable: false,
    });
    window.WebSocket = TrackedWebSocket as typeof window.WebSocket;
  });
}

async function dragSeparator(
  page: Page,
  label: "主分隔线" | "次分隔线",
  delta: { x?: number; y?: number },
): Promise<void> {
  const separator = page.getByRole("separator", { name: label });
  const before = await separator.getAttribute("aria-valuenow");
  const box = await separator.boundingBox();
  expect(box).not.toBeNull();

  await separator.dispatchEvent("pointerdown", {
    pointerId: 1,
    pointerType: "mouse",
    clientX: box!.x + box!.width / 2,
    clientY: box!.y + box!.height / 2,
    buttons: 1,
  });
  await page.evaluate(
    ({ x, y }) => {
      window.dispatchEvent(
        new PointerEvent("pointermove", {
          pointerId: 1,
          pointerType: "mouse",
          clientX: x,
          clientY: y,
          buttons: 1,
        }),
      );
      window.dispatchEvent(new PointerEvent("pointerup", { pointerId: 1, pointerType: "mouse" }));
    },
    {
      x: box!.x + box!.width / 2 + (delta.x ?? 0),
      y: box!.y + box!.height / 2 + (delta.y ?? 0),
    },
  );

  await expect(separator).not.toHaveAttribute("aria-valuenow", before ?? "");
}

async function failFirstMessageAfterCommit(route: Route): Promise<void> {
  const response = await route.fetch();
  await route.fulfill({
    status: 500,
    contentType: "application/json",
    body: JSON.stringify({
      error: { code: "forced_failure", message: "forced failure", retryable: true, details: {} },
      request_id: "playwright-forced-failure",
    }),
  });
  await response.dispose();
}

test("frontend-only flow covers draft, validate, start, mentions, retry, pause/resume, end and history", async ({
  page,
  request,
}) => {
  test.setTimeout(60_000);
  await endRunningConversations(request);
  const title = `Playwright UI ${Date.now()}`;

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "群聊实验台" })).toBeVisible();
  await expect(page.getByText("还没有凭据。若仅做验收，可继续使用 Scripted Adapter。")).toBeVisible();
  await expect(page.getByRole("separator", { name: "主分隔线" })).toBeVisible();
  await expect(page.getByRole("separator", { name: "次分隔线" })).toBeVisible();

  await dragSeparator(page, "主分隔线", { x: 120 });
  await dragSeparator(page, "次分隔线", { y: 80 });

  await page.getByLabel("Draft Title").fill(title);
  await page.getByRole("button", { name: "打开 9 步向导" }).click();
  const wizardDialog = page.getByRole("dialog", { name: "新建实验向导" });
  await expect(wizardDialog).toBeVisible();
  await expect(wizardDialog.getByRole("heading", { name: "1. 模板与策略包" })).toBeVisible();
  await expect(
    wizardDialog.getByRole("button", { name: /9\. 配置 Diff 与启动确认/ }),
  ).toBeVisible();
  await page.getByLabel("Wizard Draft Title").fill(title);
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByLabel("Wizard Agent A Name").fill("Agent Alpha");
  await page.getByLabel("Wizard Agent B Name").fill("Agent Beta");
  for (let index = 0; index < 7; index += 1) {
    await page.getByRole("button", { name: "下一步" }).click();
  }
  await expect(wizardDialog.getByRole("heading", { name: "配置 Diff", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "创建草稿并校验" }).click();

  await expect(page.locator(".chat-pane").getByRole("heading", { name: title })).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByLabel("Agent Alpha Display Name")).toBeDisabled();
  await expect(page.getByText("校验通过，可以开始实验")).toBeVisible({ timeout: 10_000 });

  await page.getByRole("button", { name: "校验配置" }).click();
  await expect(page.getByText("校验通过，可以开始实验")).toBeVisible({ timeout: 10_000 });

  await page.getByRole("button", { name: "开始实验" }).click();
  await expect(page.getByLabel("Agent Alpha Display Name")).toBeDisabled({ timeout: 10_000 });

  const composer = page.getByLabel("Public Message");
  await composer.fill("@");
  await expect(page.getByRole("listbox", { name: "Mention Suggestions" })).toBeVisible();
  await composer.press("Enter");
  await expect(composer).toHaveValue("@all ");
  await composer.type("请两位一起回答");
  await composer.press("Enter");
  await expect(page.getByText("请两位一起回答")).toBeVisible();
  await expect(page.getByText("ACK").first()).toBeVisible({ timeout: 10_000 });

  let forced = false;
  await page.route(`${apiBase}/api/v1/conversations/*/messages`, async (route) => {
    if (forced) {
      await route.continue();
      return;
    }
    forced = true;
    await failFirstMessageAfterCommit(route);
  });

  await page
    .locator("article")
    .filter({ hasText: "请两位一起回答" })
    .getByRole("button", { name: "引用" })
    .click();
  await composer.fill("补充一句");
  await composer.press("Enter");
  await expect(page.getByText("重试")).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: "重试" }).click();
  await expect(page.getByText("补充一句")).toBeVisible();
  await expect(page.getByText("回复消息")).toBeVisible({ timeout: 10_000 });

  await page.getByRole("button", { name: "暂停" }).click();
  await expect(page.locator(".status-strip").getByText(/^paused · seq \d+$/i)).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: "恢复" }).click();
  await expect(page.locator(".status-strip").getByText(/^running · seq \d+$/i)).toBeVisible({ timeout: 10_000 });

  await page.getByLabel("clarity score").fill("4");
  await page.getByRole("button", { name: "保存人工评分" }).click();
  await page.getByRole("button", { name: "生成分析 ZIP" }).click();
  await expect(page.getByText("ready")).toBeVisible({ timeout: 10_000 });

  await page.getByRole("button", { name: "结束" }).click();
  await expect(page.locator(".status-strip").getByText(/^ended · seq \d+$/i)).toBeVisible({ timeout: 10_000 });

  const conversationId = await findConversationIdByTitle(request, title);
  await markHistoryAsUnknownAdapter(conversationId);
  await page.reload();
  await page.getByRole("button", { name: new RegExp(title) }).last().click();
  await expect(page.getByText("raw fallback")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("历史档案 / Raw Fallback")).toBeVisible();
});

test("disconnect reconnects and clears transient typing state from REST authority", async ({
  page,
  request,
}) => {
  await endRunningConversations(request);
  const runningTitle = `Playwright Reconnect ${Date.now()}`;
  const conversationId = await createRuntimeConversation(request, runningTitle, [
    {
      purpose: "decision",
      agent_id: "agent-a",
      match_contains: "@A",
      delay_ms: 4000,
      output_json: { should_reply: true, reply_key: "playwright" },
    },
    {
      purpose: "action",
      agent_id: "agent-a",
      match_contains: "\"reply_key\": \"playwright\"",
      output_json: {
        content_markdown: "A playwright done",
        mentions: [],
        primary_reply_to: null,
        responds_to: [],
      },
    },
  ]);

  await installSocketTracker(page);
  await page.goto("/");
  await selectConversation(page, runningTitle);

  const composer = page.getByLabel("Public Message");
  await composer.fill("@A 请开始响应");
  await composer.press("Enter");
  await page.waitForTimeout(300);

  await page.evaluate(() => {
    const sockets = (window as typeof window & { __palChatSockets?: WebSocket[] }).__palChatSockets;
    sockets?.[0]?.close();
  });

  await expect
    .poll(
      async () =>
        page.evaluate(
          () => ((window as typeof window & { __palChatSockets?: WebSocket[] }).__palChatSockets ?? []).length,
        ),
      { timeout: 10_000 },
    )
    .toBeGreaterThan(1);

  await expect(page.getByText("A playwright done")).toBeVisible({ timeout: 10_000 });
  await expect(
    page
      .locator("section")
      .filter({ has: page.getByRole("heading", { name: "Agent A" }) })
      .getByText("idle")
      .first(),
  ).toBeVisible({ timeout: 10_000 });

  const response = await request.get(`${apiBase}/api/v1/conversations/${conversationId}`);
  expect(response.ok()).toBeTruthy();
});
