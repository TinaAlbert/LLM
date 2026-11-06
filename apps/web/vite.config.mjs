import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

const local = {
  host: '127.0.0.1', port: 5173, strictPort: true,
  allowedHosts: ['127.0.0.1'], cors: false,
  proxy: { '/api': { target: 'http://127.0.0.1:8000', rewrite: path => path.replace(/^\/api/, '') } },
};
export default defineConfig({
  server: { ...local, fs: { strict: true, allow: [fileURLToPath(new URL('.', import.meta.url))],
    deny: ['**/.env*', '**/*.{crt,pem,key}', '**/.git/**', '**/tests/**', '**/playwright.config.*'] } },
  preview: local,
  build: { sourcemap: false },
});
