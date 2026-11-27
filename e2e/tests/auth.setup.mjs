// Sign each account in once and keep the session, so the specs start signed in
// and the login rate limit (10/min per address) is spent here, not per test.
// The limit counts the form's GET as well as its POST, so these sign-ins alone
// use 8 of the 10; the counters are cleared afterwards so the login spec, which
// signs in twice more, starts with the budget a real visitor would have.
import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, test as setup } from "@playwright/test";
import { USERS, authFile, helper, signIn } from "../helpers.mjs";

setup.afterAll(() => {
  helper("reset-limits");
});

for (const who of Object.keys(USERS)) {
  setup(`sign in: ${who}`, async ({ page }) => {
    await signIn(page, USERS[who]);
    await expect(page).not.toHaveURL(/\/auth\/login/);
    mkdirSync(path.dirname(authFile(who)), { recursive: true });
    await page.context().storageState({ path: authFile(who) });
  });
}
