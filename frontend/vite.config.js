import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  // Serve MedNLP Studio từ thư mục public/
  root: 'public',
  publicDir: false,

  server: {
    port: 5173,
    proxy: {
      // Proxy API calls tới Python backend (port 8000)
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },

  build: {
    outDir: '../dist',
    emptyOutDir: true,
  },
})
