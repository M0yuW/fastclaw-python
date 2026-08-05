import { expect, test } from "@playwright/test";

test("login, Agent, chat, Stop, history, files and skills", async ({ page, request }) => {
  test.setTimeout(60_000);
  const onboard = await request.post("/api/onboard", {
    data: {
      username: "admin",
      email: "admin@example.test",
      password: "correct horse battery staple",
      displayName: "Administrator",
      provider: "fixture",
      apiBase: "http://127.0.0.1:19001/v1",
      model: "fixture/model-1",
      agentName: "E2E Analyst",
    },
  });
  expect(onboard.ok()).toBeTruthy();

  await page.goto("/");
  await page.getByPlaceholder("username or email").fill("admin");
  await page.getByPlaceholder("password").fill("correct horse battery staple");
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page).toHaveURL(/\/overview\//);

  const agentsResponse = await page.request.get("/api/agents");
  const agents = (await agentsResponse.json()).agents;
  expect(agents).toHaveLength(1);
  const agentId = agents[0].id as string;

  await page.goto("/agents/");
  await expect(page.getByText("E2E Analyst", { exact: true })).toBeVisible();
  await page.goto(`/agents/${agentId}/customize/`);
  await expect(page.getByRole("heading", { name: "Customize" })).toBeVisible();
  await page.goto(`/agents/${agentId}/skills/`);
  await expect(page.getByRole("heading", { name: "Skills" })).toBeVisible();

  await page.goto(`/agents/${agentId}/chat/`);
  const composer = page.locator("textarea");
  await composer.fill("hello");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByText("hello world", { exact: true })).toBeVisible();
  const historyUrl = page.url();
  expect(historyUrl).toContain("session=");
  await page.reload();
  await expect(page.getByText("hello world", { exact: true })).toBeVisible();

  await composer.fill("slow response");
  await page.getByRole("button", { name: "Send message" }).click();
  const stop = page.getByRole("button", { name: "Stop generating" });
  await expect(stop).toBeVisible();
  await stop.click();
  await expect(page.getByText("(Stopped)", { exact: true })).toBeVisible();
});
