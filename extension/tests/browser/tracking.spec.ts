import { test, expect } from "@playwright/test";

test("existing submission is visible before filling", async ({ page }) => {
  await page.goto("/tests/extension/panel-preview.html?reset=1&applied=1");
  const notice = page.getByRole("status", { name: "Existing application" });
  await expect(notice).toContainText("You already submitted this application.");
  await expect(notice).toContainText("Application Submitted · 2026-09-01");
  await expect(page.locator("#name")).toHaveValue("");
});

test("completed autofill saves to the board without submitting", async ({ page }) => {
  await page.goto("/tests/extension/panel-preview.html?reset=1");
  await page.locator("#jt-autofill").click();
  await expect(page.getByText("Saved to your board. Existing status and application date preserved.", { exact: true })).toBeVisible();
  await expect(page.locator("body")).toHaveAttribute("data-track-requests", "1");
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
  await expect(page.locator("#demo-submit")).toBeVisible();
});

test("a failed board save keeps the form and offers retry", async ({ page }) => {
  await page.goto("/tests/extension/panel-preview.html?reset=1&track-error=1");
  await page.locator("#jt-autofill").click();
  await expect(page.getByRole("button", { name: "Retry saving to board" })).toBeVisible();
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
  await page.getByRole("button", { name: "Retry saving to board" }).click();
  await expect(page.locator("body")).toHaveAttribute("data-track-requests", "2");
});

// Greenhouse drops a required react-select's hidden input once the person
// answers it. That changed the page signature, the fill was dropped, and
// Submit recorded a new fill with no fields beside the real one.
test("a control appearing beside the filled ones keeps the fill for Submit", async ({ page }) => {
  await page.goto("/tests/extension/panel-preview.html?reset=1");
  await page.locator("#jt-autofill").click();
  await expect(page.getByText("Saved to your board. Existing status and application date preserved.", { exact: true })).toBeVisible();
  await page.evaluate(() => document.getElementById("demo-form")!.insertAdjacentHTML("afterbegin", '<input tabindex="-1" aria-hidden="true">'));
  // The page watcher ticks every 700ms.
  await page.waitForTimeout(1500);
  await expect(page.getByText("The form moved to a new page.")).toHaveCount(0);
  await page.locator("#demo-submit").click();
  await expect(page).toHaveURL(/state=confirmed/);
  await expect(page.getByRole("button", { name: "Retry saving" })).toBeVisible();
  // The attempt carried the fill's id, so recording it opened no new fill.
  await expect(page.locator("body")).not.toHaveAttribute("data-resolve-requests", /./);
});

test("a new page under the same url offers Autofill again", async ({ page }) => {
  await page.goto("/tests/extension/panel-preview.html?reset=1");
  await page.locator("#jt-autofill").click();
  await expect(page.getByText("Saved to your board. Existing status and application date preserved.", { exact: true })).toBeVisible();
  await page.evaluate(() => {
    for (const label of document.querySelectorAll("#demo-form label")) label.remove();
    document.getElementById("demo-form")!.insertAdjacentHTML("afterbegin", '<label>Phone<input id="phone"></label>');
  });
  await expect(page.getByText("The form moved to a new page.")).toBeVisible();
});
