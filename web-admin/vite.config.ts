import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Served by the shared plugin host at /api/v1/plugins/marketing/admin/* — the
// API Gateway path, not a separate CloudFront/S3 origin. Every asset and link
// URL must carry that full prefix, INCLUDING THIS PLUGIN'S OWN NAME.
//
// idea-scout's copy of this file records why that sentence is shouted: it was
// copied from ideation and kept ideation's base, so the built index.html asked
// for idea-scout's asset filenames under /api/v1/plugins/ideation/admin/ — 503,
// blank page, and no local gate caught it. Lint, typecheck, tests and the
// production build all pass, because `base` only affects URLs inside the
// emitted HTML. base-path.test.ts is what actually checks it.
export default defineConfig({
  base: '/api/v1/plugins/marketing/admin/',
  plugins: [react()],
  build: { outDir: 'dist' },
  test: { environment: 'jsdom', globals: true, setupFiles: ['./src/test-setup.ts'] },
})
