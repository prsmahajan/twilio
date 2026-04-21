import { defineConfig } from 'vite';

const FLASK = 'http://localhost:5000';

export default defineConfig({
  server: {
    port: 3000,
    proxy: {
      '/token':    { target: FLASK, changeOrigin: true },
      '/connect':  { target: FLASK, changeOrigin: true },
      '/threads':  { target: FLASK, changeOrigin: true },
      '/messages': { target: FLASK, changeOrigin: true },
      '/send_sms': { target: FLASK, changeOrigin: true },
      '/recent':   { target: FLASK, changeOrigin: true },
    },
  },
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
  },
});
