// An HTMX form posting with the CSRF token in a header. When that token went
// stale the box used to do nothing at all (PRELAUNCH Y3); the unit suite runs
// with CSRF off, so only a real browser against a real session exercises it.
import { expect, test } from "@playwright/test";
import { authFile } from "../helpers.mjs";

test.use({ storageState: authFile("reader") });

test("an interest added without a page load is saved", async ({ page }) => {
  const keyword = `graphene e2e ${Date.now()}`;

  await page.goto("/");
  const manager = page.locator("#interests-manager");
  await manager.locator('input[name="value"]').fill(keyword);

  const saved = page.waitForResponse(
    (r) => r.url().endsWith("/interests/add") && r.request().method() === "POST",
  );
  await manager.locator('input[name="value"]').press("Enter");
  expect((await saved).status()).toBe(200);

  await expect(page.locator("#interests-manager")).toContainText(keyword);

  await page.reload();
  await expect(page.locator("#interests-manager")).toContainText(keyword);
});
