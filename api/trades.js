const fs = require('fs');
const path = require('path');

function readLocalTradesFallback() {
  const tradesPath = path.join(process.cwd(), 'trades.json');
  if (!fs.existsSync(tradesPath)) return [];
  const data = fs.readFileSync(tradesPath, 'utf-8');
  const parsed = JSON.parse(data);
  return Array.isArray(parsed) ? parsed : (parsed.trades || []);
}

export default async function handler(req, res) {
  try {
    const supabaseUrl = process.env.SUPABASE_URL;
    const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY;

    // If Supabase is not configured yet, gracefully fall back.
    if (!supabaseUrl || !serviceRoleKey) {
      return res.status(200).json(readLocalTradesFallback());
    }

    const query = '/rest/v1/trades?select=*&order=entry_time.desc.nullslast,created_at.desc.nullslast';
    const response = await fetch(`${supabaseUrl}${query}`, {
      headers: {
        apikey: serviceRoleKey,
        Authorization: `Bearer ${serviceRoleKey}`,
        'Content-Type': 'application/json'
      }
    });

    if (!response.ok) {
      const fallback = readLocalTradesFallback();
      return res.status(200).json(fallback);
    }

    const trades = await response.json();
    res.status(200).json(Array.isArray(trades) ? trades : []);
  } catch (error) {
    console.error('Error reading trades:', error);
    // Last-resort fallback
    try {
      return res.status(200).json(readLocalTradesFallback());
    } catch {
      return res.status(500).json({ error: 'Failed to read trades' });
    }
  }
}
