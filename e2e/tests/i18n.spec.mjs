// Language selection once did nothing at all: Babel was wired with a 2.x API,
// so ?lang=, the cookie and the user's saved locale were all ignored, and the
// default happened to be Turkish so nobody noticed (docs/HANDOVER.md §5.6).
import { expect, test } from "@playwright/test";
import { authFile } from "../helpers.mjs";

test.use({ storageState: authFile("polyglot") });

// By its accessible name, which is the translated aria-label: the topbar has
// other labelled dropdowns (the notification bell), so "the first dropdown
// with an aria-label" is not this button.
const languageButton = (page, name) => page.getByRole("button", { name, exact: true });

test("switching to English changes the UI and follows the user to a new session", async ({
  page,
  browser,
}) => {
  await page.goto("/library/");
  await expect(page.locator("html")).toHaveAttribute("lang", "tr");
  await expect(languageButton(page, "Dili değiştir")).toBeVisible();

  await languageButton(page, "Dili değiştir").click();
  await page.getByRole("link", { name: "EN", exact: true }).click();

  // Back on the page it was changed from, now in English.
  await expect(page).toHaveURL(/\/library\//);
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(languageButton(page, "Change language")).toBeVisible();

  // A fresh browser has no language cookie; the choice was saved to the user.
  const fresh = await browser.newContext({ storageState: authFile("polyglot") });
  const again = await fresh.newPage();
  await again.goto("/library/");
  await expect(again.locator("html")).toHaveAttribute("lang", "en");
  await fresh.close();
});
