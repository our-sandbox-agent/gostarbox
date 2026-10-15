import { Pool } from 'pg'
import { PostgresStore } from './postgres.js'

if (!process.env.DATABASE_URL) throw new Error('DATABASE_URL required for explicit migration')
const pool = new Pool({connectionString: process.env.DATABASE_URL})
try {
  await new PostgresStore(pool).migrate()
  console.log('Control-plane migrations applied')
} finally { await pool.end() }
