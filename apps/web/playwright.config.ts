import { defineConfig } from '@playwright/test';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../..', import.meta.url));
export default defineConfig({
  testDir: './tests', fullyParallel: false, workers: 1, retries: 0,
  timeout: 180000, expect: {timeout: 20000}, reporter: 'list',
  use: {baseURL:'http://127.0.0.1:5173', browserName:'chromium', channel:'msedge',
    actionTimeout:20000, navigationTimeout:20000, trace:'off', screenshot:'only-on-failure', video:'off'},
  webServer: [
    {command: '.venv\\Scripts\\python.exe -X utf8 -m services.business.scripts.browser_server',
      cwd:root, url:'http://127.0.0.1:8000/session', reuseExistingServer:false, timeout:30000},
    {command:'npm.cmd run dev', url:'http://127.0.0.1:5173', reuseExistingServer:false, timeout:30000},
  ],
});
