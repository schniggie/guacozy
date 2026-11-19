import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {defineConfig} from 'vite'
import react from '@vitejs/plugin-react'

const frontendRoot = path.dirname(fileURLToPath(import.meta.url))
const sharedCssDir = path.resolve(frontendRoot, '../guacozy_server/static/css')

const host = process.env.DJANGO_PROXY_HOST || 'localhost'
const port = process.env.DJANGO_PROXY_PORT || '8000'
const httpTarget = `http://${host}:${port}`
const wsTarget = `ws://${host}:${port}`

export default defineConfig({
  plugins: [react()],
  // Relative asset URLs so nginx can serve the build from /cozy/.
  base: './',
  build: {
    outDir: 'build',
    emptyOutDir: true,
  },
  server: {
    host: '0.0.0.0',
    port: 3000,
    fs: {
      allow: [frontendRoot, sharedCssDir],
    },
    // Keep the browser Host. A string target makes Vite rewrite Host to
    // port 8000, and Django then rejects the login POST as a bad origin.
    proxy: {
      '/api': {target: httpTarget, changeOrigin: false},
      '/admin': {target: httpTarget, changeOrigin: false},
      '/accounts': {target: httpTarget, changeOrigin: false},
      '/staticfiles': {target: httpTarget, changeOrigin: false},
      '/tunnelws': {target: wsTarget, ws: true, changeOrigin: false},
    },
  },
  define: {
    'process.env.REACT_APP_VERSION': JSON.stringify(process.env.REACT_APP_VERSION || ''),
  },
})
