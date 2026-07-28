import { defineConfig } from 'vite';

// Must match PORT in .env. Default is 5055, not 5000 — on macOS the AirPlay
// Receiver squats on 5000 and answers 403, which surfaces as "Init failed: HTTP 403".
const FLASK = process.env.FLASK_URL || 'http://localhost:5055';

export default defineConfig({
  server: {
    port: 3000,
    proxy: {
      '/api':          { target: FLASK, changeOrigin: true },
      '/token':        { target: FLASK, changeOrigin: true },
      '/connect':      { target: FLASK, changeOrigin: true },
      '/incoming':     { target: FLASK, changeOrigin: true },
      '/call_status':  { target: FLASK, changeOrigin: true },
      '/dispositions': { target: FLASK, changeOrigin: true },
      '/threads':      { target: FLASK, changeOrigin: true },
      '/messages':     { target: FLASK, changeOrigin: true },
      '/send_sms':     { target: FLASK, changeOrigin: true },
      '/recent':       { target: FLASK, changeOrigin: true },
    },
  },
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
  },
});
