// End-to-end smoke suite. Run it through `python scripts/e2e.py`, which starts
// the database, the server and the worker this config expects; see that file
// for why the suite exists and what it deliberately does not touch.
import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.E2E_BASE_URL;
const stubPort = process.env.E2E_STUB_PORT;
if (!baseURL || !stubPort) {
  throw new Error("Run the e2e suite through `python scripts/e2e.py`, not `npx playwright test`.");
}

export default defineConfig({
  testDir: "./tests",
  outputDir: "./.artifacts/test-results",
  // One worker: the specs share one Celery worker on a solo pool, and the
  // alert spec waits on it. Parallel specs would only race for that slot.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  forbidOnly: !!process.env.CI,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [
    ["list"],
    ["html", { outputFolder: "./.artifacts/report", open: "never" }],
  ],
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "node stub_site.mjs",
    url: `http://127.0.0.1:${stubPort}/robots.txt`,
    reuseExistingServer: false,
  },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.mjs/ },
    {
      name: "smoke",
      testMatch: /\.spec\.mjs/,
      dependencies: ["setup"],
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
