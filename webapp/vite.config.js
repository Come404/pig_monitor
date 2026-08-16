import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // api.js uses relative paths (same-origin in production, served by
    // FastAPI). Proxy them to a locally running backend so `npm run dev`
    // still works standalone -- see README for `uvicorn ... --port 8000`.
    proxy: {
      '/health': 'http://localhost:8000',
      '/run': 'http://localhost:8000',
      '/report': 'http://localhost:8000',
    },
  },
})