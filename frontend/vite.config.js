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
    proxy: {
      '/api': httpTarget,
      '/admin': httpTarget,
      '/accounts': httpTarget,
      '/staticfiles': httpTarget,
      '/tunnelws': {target: wsTarget, ws: true},
    },
  },
  define: {
    'process.env.REACT_APP_VERSION': JSON.stringify(process.env.REACT_APP_VERSION || ''),
  },
})
