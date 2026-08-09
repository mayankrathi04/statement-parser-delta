import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // Built assets land inside the package so `pip install` ships the UI with it.
  build: { outDir: '../sparser/web/dist', emptyOutDir: true },
  server: { proxy: { '/api': 'http://127.0.0.1:8770' } },
})
