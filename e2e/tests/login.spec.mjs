import { expect, test } from "@playwright/test";
import { USERS, signIn } from "../helpers.mjs";

test("a wrong password keeps you out, the right one lets you in, sign-out ends it", async ({
  page,
}) => {
  const { reader } = USERS;

  await signIn(page, { username: reader.username, password: "not-the-password" });
  await expect(page).toHaveURL(/\/auth\/login/);
  await expect(page.locator(".alert-danger")).toBeVisible();

  await signIn(page, reader);
  await expect(page).not.toHaveURL(/\/auth\/login/);
  // The topbar names the signed-in user: the session really belongs to them.
  await expect(page.getByRole("button", { name: reader.fullName })).toBeVisible();

  await page.goto("/auth/logout");
  await page.goto("/library/");
  await expect(page).toHaveURL(/\/auth\/login/);
});
