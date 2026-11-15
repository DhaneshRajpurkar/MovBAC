// Vercel serverless function: GET /api/weather?lat=..&lon=.. → current conditions + 5-day summary from
// OpenWeather, for the map popups. The key stays on the server (OPENWEATHER_KEY env var). Same answer as server.py.
const cache = new Map();   // "lat,lon" rounded to 0.1° -> {t, out}

module.exports = async (req, res) => {
  const key = (process.env.OPENWEATHER_KEY || '').trim();
  if (!key) return res.status(500).json({ error: 'OPENWEATHER_KEY missing' });
  const lat = Math.round(+req.query.lat * 10) / 10, lon = Math.round(+req.query.lon * 10) / 10;
  if (!(Math.abs(lat) <= 90 && Math.abs(lon) <= 180)) return res.status(400).json({ error: 'need lat=..&lon=..' });
  const id = `${lat},${lon}`, hit = cache.get(id);
  if (hit && Date.now() - hit.t < 600e3) return res.status(200).json(hit.out);
  const q = `lat=${lat}&lon=${lon}&units=metric&appid=${key}`;
  try {
    const get = async u => { const r = await fetch(u); if (!r.ok) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`); return r.json(); };
    const [now, fc] = await Promise.all([
      get(`https://api.openweathermap.org/data/2.5/weather?${q}`),
      get(`https://api.openweathermap.org/data/2.5/forecast?${q}`)]);
    const s = fc.list || [];
    const out = {
      place: now.name || '', description: ((now.weather || [])[0] || {}).description || '',
      temp_c: now.main.temp, humidity: now.main.humidity,
      wind_ms: (now.wind || {}).speed || 0, gust_ms: (now.wind || {}).gust ?? null,
      rain_1h_mm: (now.rain || {})['1h'] || 0,
      next5: {
        max_temp_c: s.length ? Math.max(...s.map(x => x.main.temp_max)) : null,
        min_humidity: s.length ? Math.min(...s.map(x => x.main.humidity)) : null,
        max_wind_ms: s.length ? Math.max(...s.map(x => (x.wind || {}).speed || 0)) : null,
        rain_mm: Math.round(s.reduce((a, x) => a + ((x.rain || {})['3h'] || 0), 0) * 10) / 10,
      },
      source: 'OpenWeather',
    };
    if (cache.size > 2000) cache.clear();
    cache.set(id, { t: Date.now(), out });
    res.status(200).json(out);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e).split(key).join('<key>').slice(0, 200) });
  }
};
