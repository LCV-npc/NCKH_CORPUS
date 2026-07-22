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
      // Proxy tới Spring Boot backend (port 8080) nếu cần
      '/spring': {
        target: 'http://localhost:8080',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/spring/, ''),
      },
    },
  },

  build: {
    outDir: '../dist',
    emptyOutDir: true,
  },
})
