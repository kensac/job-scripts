import { test, expect } from "@playwright/test";

const preview = "/tests/extension/panel-preview.html?reset=1";
async function ready(page: import("@playwright/test").Page, extra = "") {
  await page.goto(preview + extra);
  await page.locator("#jt-autofill").click();
  await expect(page.getByRole("searchbox", { name: "Search application answers" })).toBeVisible();
}
const card = (page: import("@playwright/test").Page, name: string) => page.locator(".answer-card").filter({ has: page.getByRole("heading", { name, exact: true }) });

test("remaining fields link to controls and disappear as the person fills them", async ({ page }) => {
  await ready(page);
  const remaining = page.getByRole("region", { name: "Fields needing attention" });
  await expect(remaining.getByRole("button")).toHaveCount(2);
  await remaining.getByRole("button", { name: "Portfolio URL Unanswered", exact: true }).click();
  await expect(page.locator("#portfolio")).toBeFocused();
  await page.locator("#portfolio").fill("https://example.com/portfolio");
  await page.getByRole("button", { name: "Expand panel", exact: true }).click();
  await expect(remaining.getByRole("button")).toHaveCount(1);
  await expect(remaining.getByRole("button", { name: "Resume / CV Unanswered", exact: true })).toBeVisible();
});

test("remaining fields stay navigable when saved-answer loading fails", async ({ page }) => {
  await page.goto(preview + "&review-error=1");
  await page.locator("#jt-autofill").click();
  const remaining = page.getByRole("region", { name: "Fields needing attention" });
  await remaining.getByRole("button", { name: "Portfolio URL Unanswered", exact: true }).click();
  await expect(page.locator("#portfolio")).toBeFocused();
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
});

test("edit one answer, persist suggestions, and leave other fields unchanged", async ({ page }) => {
  await ready(page);
  const name = card(page, "Full name");
  await name.getByText("Edit or improve answer", { exact: true }).click();
  await name.getByRole("textbox", { name: "Answer for Full name", exact: true }).fill("Alex M.");
  await name.getByRole("textbox", { name: "Suggestions for Full name", exact: true }).fill("Use my preferred name");
  await name.getByRole("button", { name: "Apply this answer", exact: true }).click();
  await expect(page.locator("#name")).toHaveValue("Alex M.");
  await expect(page.locator("#email")).toHaveValue("alex@example.com");
  await expect(name.getByRole("textbox", { name: "Suggestions for Full name", exact: true })).toHaveValue("Use my preferred name");
});

test("generation previews a draft without changing the form", async ({ page }) => {
  await ready(page);
  const name = card(page, "Full name");
  await name.getByText("Edit or improve answer", { exact: true }).click();
  await name.getByRole("button", { name: "Generate new draft", exact: true }).click();
  await expect(page.getByText("New draft ready. Review it, then choose Apply this answer.", { exact: true })).toBeVisible();
  await expect(name.getByRole("textbox", { name: "Answer for Full name", exact: true })).toHaveValue("A revised answer based on my backend project.");
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
});

test("existing manual answers survive autofill and a field can be refilled explicitly", async ({ page }) => {
  await page.goto(preview);
  await page.locator("#name").fill("My own answer");
  await page.locator("#jt-autofill").click();
  await expect(page.getByRole("searchbox", { name: "Search application answers" })).toBeVisible();
  await expect(page.locator("#name")).toHaveValue("My own answer");
  const name = card(page, "Full name");
  await name.getByText("Edit or improve answer", { exact: true }).click();
  await name.getByRole("button", { name: "Refill saved answer", exact: true }).click();
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
});

test("search and unanswered filtering operate across the read form", async ({ page }) => {
  await ready(page);
  await page.getByRole("searchbox", { name: "Search application answers" }).fill("linkedin");
  await expect(page.locator(".answer-card:visible")).toHaveCount(1);
  await page.getByRole("searchbox", { name: "Search application answers" }).fill("");
  await page.getByRole("combobox", { name: "Filter application answers" }).selectOption("blank");
  await expect(page.locator(".answer-card:visible")).toHaveCount(2);
});

test("a failed save never changes the employer form", async ({ page }) => {
  await ready(page, "&save-error=1");
  const name = card(page, "Full name");
  await name.getByText("Edit or improve answer", { exact: true }).click();
  await name.getByRole("textbox", { name: "Answer for Full name", exact: true }).fill("Not saved");
  await name.getByRole("button", { name: "Apply this answer", exact: true }).click();
  await expect(page.getByText("Could not save your draft. The form has not changed.", { exact: true })).toBeVisible();
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
  await expect(name.getByRole("textbox", { name: "Answer for Full name", exact: true })).toHaveValue("Not saved");
});

test("review loading failure offers retry rather than an empty field list", async ({ page }) => {
  await page.goto(preview + "&review-error=1");
  await page.locator("#jt-autofill").click();
  await expect(page.getByRole("button", { name: "Retry loading answers", exact: true })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("Could not load saved answers");
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
});

test("typing during an active fill stops automation and preserves the edit", async ({ page }) => {
  await page.goto(preview + "&slow=1");
  await page.locator("#jt-autofill").click();
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
  await page.locator("#email").fill("my-edit@example.com");
  await expect(page.locator("#jt-apply")).toContainText("Autofill stopped");
  await page.waitForTimeout(700);
  await expect(page.locator("#email")).toHaveValue("my-edit@example.com");
});

test("a scripted checkbox change does not masquerade as a manual edit", async ({ page }) => {
  await page.goto(preview + "&slow=1");
  await page.evaluate(() => { const box = document.createElement("input"); box.type = "checkbox"; box.id = "scripted-check"; document.querySelector("main")!.append(box); });
  await page.locator("#jt-autofill").click();
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
  await page.evaluate(() => (document.querySelector("#scripted-check") as HTMLInputElement).click());
  await expect(page.locator("#email")).toHaveValue("alex@example.com");
  await expect(page.locator("#jt-again")).toBeVisible();
});

for (const theme of ["light", "dark"]) test(`${theme} review preserves unsaved text across theme and minimise`, async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await ready(page, `&theme=${theme}`);
  const name = card(page, "Full name");
  await name.getByText("Edit or improve answer", { exact: true }).click();
  const input = name.getByRole("textbox", { name: "Answer for Full name", exact: true });
  await input.fill("Unfinished edit");
  await page.getByRole("searchbox", { name: "Search application answers" }).fill("email");
  await page.getByRole("searchbox", { name: "Search application answers" }).fill("");
  await expect(input).toHaveValue("Unfinished edit");
  await page.locator("#jt-theme").click();
  await page.getByRole("button", { name: "Minimise panel", exact: true }).click();
  await page.getByRole("button", { name: "Expand panel", exact: true }).click();
  await expect(input).toHaveValue("Unfinished edit");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: `test-results/review-${theme}.png`, fullPage: true });
});
