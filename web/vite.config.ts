import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Served by the shared plugin host at /api/v1/plugins/marketing/ui/* — the API
// Gateway path, not a separate origin. Every asset URL must carry that full
// prefix, INCLUDING THIS PLUGIN'S OWN NAME (see web-admin/vite.config.ts for
// the incident behind that sentence). base-path.test.ts is what checks it.
export default defineConfig({
  base: '/api/v1/plugins/marketing/ui/',
  plugins: [react()],
  build: { outDir: 'dist' },
  test: { environment: 'jsdom', globals: true, setupFiles: ['./src/test-setup.ts'] },
})
