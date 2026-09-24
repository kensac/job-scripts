import { test, expect } from "@playwright/test";

for (const adapter of ["ashby", "lever", "greenhouse", "recipe"]) {
  for (const prefocused of [false, true]) {
    test(`${adapter} commits controlled fields (already focused: ${prefocused})`, async ({ page }) => {
      await page.goto(`/tests/extension/field-commit.html?adapter=${adapter}${prefocused ? "&prefocused=1" : ""}`);
      if (prefocused) await page.locator("#email").focus();
      await page.getByRole("button", { name: "Fill fixture" }).click();
      await expect(page.locator("#done")).toHaveText("Done");
      await expect(page.locator("#email")).toHaveValue("alex@example.com");
      await expect(page.locator("#email-committed")).toHaveText("alex@example.com");
      if (!prefocused) await expect(page.locator("#name-committed")).toHaveText("Alex");
      await expect(page.locator("#email-error")).toBeEmpty();
      await expect(page.locator("#name-error")).toBeEmpty();
      await expect(page.locator("#name")).not.toBeFocused();
      await expect(page.locator("#email")).not.toBeFocused();
    });
  }
}
