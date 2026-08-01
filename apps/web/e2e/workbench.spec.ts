import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";

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

async function createConversation(
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
  const payload = (await response.json()) as {
    items: Array<{ id: string; status: string }>;
  };
  for (const item of payload.items) {
    if (item.status === "running") {
      expect((await request.post(`${apiBase}/api/v1/conversations/${item.id}/end`)).ok()).toBeTruthy();
    }
  }
}

async function endConversation(request: APIRequestContext, conversationId: string): Promise<void> {
  expect((await request.post(`${apiBase}/api/v1/conversations/${conversationId}/end`)).ok()).toBeTruthy();
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

async function postMention(request: APIRequestContext, conversationId: string): Promise<void> {
  const response = await request.post(`${apiBase}/api/v1/conversations/${conversationId}/messages`, {
    data: {
      client_message_id: `playwright-${Date.now()}`,
      content_markdown: "@A 请开始响应",
      mentions: ["agent-a"],
      responds_to: [],
      primary_reply_to: null,
    },
  });
  expect(response.ok()).toBeTruthy();
}

async function selectConversation(page: Page, title: string): Promise<void> {
  await page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: "实验列表" }) })
    .getByRole("button", { name: new RegExp(title) })
    .click();
}

async function tabTo(page: Page, target: Locator, maxSteps = 20): Promise<void> {
  for (let step = 0; step < maxSteps; step += 1) {
    await page.keyboard.press("Tab");
    if (await target.evaluate((node) => node === document.activeElement)) {
      return;
    }
  }
  throw new Error("target never received keyboard focus");
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

test("desktop workbench supports review mode, raw fallback and keyboard navigation", async ({
  page,
  request,
}) => {
  await endRunningConversations(request);
  const runningTitle = `Playwright Live ${Date.now()}`;
  const historyTitle = `Playwright History ${Date.now()}`;
  const historyConversationId = await createConversation(request, historyTitle, []);
  await endConversation(request, historyConversationId);
  await markHistoryAsUnknownAdapter(historyConversationId);
  await createConversation(request, runningTitle, []);

  await page.goto("/");
  await selectConversation(page, runningTitle);

  expect(page.viewportSize()).toEqual({ width: 1440, height: 900 });
  await expect(page.getByRole("tablist", { name: "Inspector Tabs" })).toBeVisible();
  await expect(page.getByRole("log")).toBeVisible();

  const agentATab = page.getByRole("tab", { name: "A 私有视图" });
  await tabTo(page, agentATab);
  await agentATab.press("Enter");
  await expect(agentATab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText(/Raw Memory JSON|Graph Memory/)).toBeVisible();

  const memoryRevision = page.getByRole("button", { name: /mem-0/ });
  await tabTo(page, memoryRevision);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("alert")).toContainText("回看模式");
  await expect(page.getByLabel("Public Message")).toBeDisabled();

  await page.getByRole("button", { name: new RegExp(`${historyTitle}.*raw fallback`) }).click();
  await page.getByRole("tab", { name: "Server 公共视图" }).click();
  await expect(page.getByRole("heading", { name: "历史档案 / Raw Fallback" })).toBeVisible();
});

test("disconnect reconnects and clears transient typing state from REST authority", async ({
  page,
  request,
}) => {
  await endRunningConversations(request);
  const runningTitle = `Playwright Reconnect ${Date.now()}`;
  const conversationId = await createConversation(request, runningTitle, [
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
  await page.getByRole("tab", { name: "A 私有视图" }).click();

  await postMention(request, conversationId);
  await page.waitForTimeout(300);

  await page.evaluate(() => {
    const sockets = (window as typeof window & { __palChatSockets?: WebSocket[] }).__palChatSockets;
    sockets?.[0]?.close();
  });

  await expect
    .poll(
      async () =>
        page.evaluate(
          () =>
            ((window as typeof window & { __palChatSockets?: WebSocket[] }).__palChatSockets ?? [])
              .length,
        ),
      { timeout: 10_000 },
    )
    .toBeGreaterThan(1);
  await expect(page.getByRole("log").getByText("A playwright done")).toBeVisible({
    timeout: 10_000,
  });
  await page.getByRole("tab", { name: "A 私有视图" }).click();
  await expect(page.getByText("idle")).toBeVisible({ timeout: 10_000 });
});
