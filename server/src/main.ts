import { serve } from '@hono/node-server'
import { createApp } from './app.js'
import { MemoryAdapter } from './store.js'

const port = Number(process.env.PORT ?? 8787)
if (!Number.isInteger(port) || port <= 0 || port > 65535) {
  throw new Error('PORT must be an integer between 1 and 65535')
}
// Loopback only: single trusted entry per the contract auth binding
// (SANDBOX_TOKEN is read inside createApp and never logged).
const app = createApp(new MemoryAdapter())
serve({ fetch: app.fetch, hostname: '127.0.0.1', port }, (info) => {
  console.log(`control-plane listening on http://127.0.0.1:${info.port}`)
})
