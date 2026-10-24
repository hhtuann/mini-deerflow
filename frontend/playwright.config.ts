import { defineConfig } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Inherited by the API and test worker. Nothing is seeded before the cold test.
process.env.MINI_DEERFLOW_E2E_STORAGE ??= join(tmpdir(), `mini-deerflow-browser-${randomUUID()}`);

export default defineConfig({
  testDir: './e2e', fullyParallel: false, workers: 1, timeout: 90_000,
  expect: { timeout: 15_000 }, reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:5173', browserName: 'chromium', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: [
    { command: 'uv run python frontend/e2e/serve_api.py', cwd: '..', url: 'http://127.0.0.1:8000/api/health', reuseExistingServer: false, timeout: 120_000 },
    { command: 'npm run dev', url: 'http://127.0.0.1:5173', reuseExistingServer: false, timeout: 60_000 },
  ],
});
