import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// Must match USERS / PASSWORD in scripts/e2e_helper.py.
export const PASSWORD = "e2e-password-1234";
export const USERS = {
  admin: { username: "admin", password: "admin1234" },
  reader: { username: "e2e-reader", password: PASSWORD, fullName: "E2E Reader" },
  researcher: { username: "e2e-researcher", password: PASSWORD, fullName: "E2E Researcher" },
  polyglot: { username: "e2e-polyglot", password: PASSWORD, fullName: "E2E Polyglot" },
};

export const authFile = (who) => path.join(ROOT, "e2e", ".artifacts", "auth", `${who}.json`);

export const stubUrl = (pathname) => `http://127.0.0.1:${process.env.E2E_STUB_PORT}${pathname}`;

/** Sign in through the real form: CSRF token, password check, session cookie. */
export async function signIn(page, { username, password }) {
  await page.goto("/auth/login");
  const form = page.locator("form").filter({ has: page.locator('input[name="password"]') });
  await form.locator('input[name="username"]').fill(username);
  await form.locator('input[name="password"]').fill(password);
  await form.locator('[type="submit"]').click();
}

/** Run a scripts/e2e_helper.py command; returns its last stdout line parsed as JSON. */
export function helper(...args) {
  const out = execFileSync(process.env.E2E_PYTHON, ["scripts/e2e_helper.py", ...args], {
    cwd: ROOT,
    env: process.env,
    encoding: "utf-8",
  });
  const lines = out.trim().split(/\r?\n/);
  return JSON.parse(lines[lines.length - 1]);
}
