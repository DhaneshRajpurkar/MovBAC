// Vercel serverless function: GET /api/hotspots?bbox=W,S,E,N&start=YYYY-MM-DD&end=YYYY-MM-DD&limit=5000
// Same behavior as server.py. The key comes from the FIRMS_MAP_KEY environment variable
// (Vercel → Project → Settings → Environment Variables), never from the website code.
const BASE = 'https://firms.modaps.eosdis.nasa.gov/api';
const MAX_DAYS = 366, CHUNK = 5, DAY = 864e5;   // FIRMS area API allows 1–5 days per request
// Products per instrument, in order of preference: each day uses the first one whose availability
// window covers it (one satellite per instrument per day, so no duplicate dots). VIIRS falls back to
// NOAA-20 / NOAA-21 when S-NPP has no data (e.g. S-NPP NRT ended 2026-11-02).
const SENSORS = {
  MODIS: ['MODIS_SP', 'MODIS_NRT'],
  VIIRS: ['VIIRS_SNPP_SP', 'VIIRS_SNPP_NRT', 'VIIRS_NOAA20_SP', 'VIIRS_NOAA20_NRT', 'VIIRS_NOAA21_SP', 'VIIRS_NOAA21_NRT'],
};
const SAT_NAMES = { T: 'Terra', A: 'Aqua', N: 'S-NPP', N20: 'NOAA-20', N21: 'NOAA-21' };
const cache = new Map();
let avail = { t: 0, data: {} };

const iso = d => d.toISOString().slice(0, 10);
const parseDate = s => { const d = new Date(s + 'T00:00:00Z'); if (isNaN(d)) throw new Error('bad date'); return d; };

function parseCSV(text) {
  const lines = text.trim().split(/\r?\n/);
  const head = lines.shift().split(',');
  return lines.map(l => { const v = l.split(','); const o = {}; head.forEach((h, i) => o[h] = v[i]); return o; });
}

async function availability(key) {
  if (Date.now() - avail.t < 6 * 3600e3 && Object.keys(avail.data).length) return avail.data;
  const data = {};
  try {
    const r = await fetch(`${BASE}/data_availability/csv/${key}/all`);
    for (const row of parseCSV(await r.text())) {
      if (row.data_id && row.max_date) data[row.data_id] = [parseDate(row.min_date), parseDate(row.max_date)];
    }
  } catch (e) { console.warn('data_availability failed', e.message); }
  if (Object.keys(data).length) avail = { t: Date.now(), data };   // don't cache a failure
  return data;
}

function pickProduct(products, d, av) {
  if (!Object.keys(av).length) return d <= new Date(Date.now() - 90 * DAY) ? products[0] : products[1];
  for (const p of products) if (av[p] && av[p][0] <= d && d <= av[p][1]) return p;
  return null;
}

function planRequests(start, end, av) {
  const jobs = [];
  for (const [inst, products] of Object.entries(SENSORS)) {
    let d = start;
    while (d <= end) {
      const src = pickProduct(products, d, av);
      if (!src) { d = new Date(+d + DAY); continue; }
      let stop = d;
      while (+stop + DAY <= +end && Math.round((stop - d) / DAY) + 1 < CHUNK &&
             pickProduct(products, new Date(+stop + DAY), av) === src) stop = new Date(+stop + DAY);
      jobs.push([inst, src, d, Math.round((stop - d) / DAY) + 1]);
      d = new Date(+stop + DAY);
    }
  }
  return jobs;
}

function norm(r, inst) {
  let conf = r.confidence;
  if (inst === 'MODIS' && !isNaN(parseFloat(conf))) conf = Math.round(parseFloat(conf));
  return {
    latitude: +r.latitude, longitude: +r.longitude, acq_date: r.acq_date,
    acq_time: String(r.acq_time || '').padStart(4, '0'), instrument: inst,
    satellite: SAT_NAMES[r.satellite] || r.satellite,
    confidence: conf, frp: parseFloat(r.frp) || 0, daynight: r.daynight || ''
  };
}

async function fetchChunk([inst, src, d, days], area, key) {
  const url = `${BASE}/area/csv/${key}/${src}/${area}/${days}/${iso(d)}`;
  const hit = cache.get(url);
  if (hit && (Date.now() - hit.t < 3600e3 || src.endsWith('_SP'))) return [inst, hit.rows, null];
  try {
    const text = await (await fetch(url)).text();
    if (!text.trimStart().toLowerCase().startsWith('latitude')) return [inst, [], `${src} ${iso(d)}: ${text.trim().slice(0, 120).split(key).join('<key>')}`];
    const rows = parseCSV(text).filter(r => [undefined, '', '0'].includes(r.type))   // vegetation fires only
      .filter(r => !isNaN(parseFloat(r.latitude)) && !isNaN(parseFloat(r.longitude)));
    if (rows.length < 20000) cache.set(url, { t: Date.now(), rows });                 // bound memory use
    return [inst, rows, null];
  } catch (e) { return [inst, [], `${src} ${iso(d)}: ${e.message}`]; }
}

// Grid square size (degrees) so the area is about 200 squares across.
function gridCell([w, s, e, n]) {
  const span = Math.max(e - w, n - s);
  for (const c of [0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1]) if (c >= span / 200) return c;
  return 1;
}

async function pool(items, n, fn) {
  const out = new Array(items.length); let i = 0;
  await Promise.all(Array.from({ length: n }, async () => { while (i < items.length) { const k = i++; out[k] = await fn(items[k]); } }));
  return out;
}

module.exports = async (req, res) => {
  const key = (process.env.FIRMS_MAP_KEY || '').trim();
  if (!key) return res.status(500).json({ error: 'FIRMS_MAP_KEY environment variable not set' });
  let bbox, start, end, limit;
  try {
    bbox = String(req.query.bbox).split(',').map(Number);
    if (bbox.length !== 4 || bbox.some(isNaN) || bbox[0] >= bbox[2] || bbox[1] >= bbox[3]) throw 0;
    start = parseDate(req.query.start);
    end = new Date(Math.min(+parseDate(req.query.end), +parseDate(iso(new Date()))));
    limit = Math.max(1, Math.min(parseInt(req.query.limit || '5000', 10) || 5000, 50000));
    if (start > end) throw 0;
  } catch { return res.status(400).json({ error: 'need bbox=W,S,E,N&start=YYYY-MM-DD&end=YYYY-MM-DD' }); }
  if ((end - start) / DAY + 1 > MAX_DAYS) start = new Date(+end - (MAX_DAYS - 1) * DAY);

  const area = bbox.map(v => v.toFixed(4)).join(',');
  const cell = gridCell(bbox);
  const jobs = planRequests(start, end, await availability(key));
  // Each answer is summarized as soon as it arrives: every detection goes into the grid, and a
  // uniform random sample of `limit` rows is kept for the dots/CSV ("reservoir sampling").
  const grid = new Map(), sample = [], warnings = [];
  let total = 0;
  await pool(jobs, 6, async j => {
    const [inst, rows, err] = await fetchChunk(j, area, key);
    if (err) warnings.push(err);
    for (const r of rows) {
      total++;
      const frp = parseFloat(r.frp) || 0;
      const k = Math.floor(+r.longitude / cell) + ',' + Math.floor(+r.latitude / cell);
      const g = grid.get(k);
      if (!g) grid.set(k, [1, frp, frp]); else { g[0]++; g[1] += frp; if (frp > g[2]) g[2] = frp; }
      if (sample.length < limit) sample.push([inst, r]);
      else { const i = Math.floor(Math.random() * total); if (i < limit) sample[i] = [inst, r]; }
    }
  });
  const rows = sample.map(([inst, r]) => norm(r, inst));
  rows.sort((a, b) => (a.acq_date + a.acq_time).localeCompare(b.acq_date + b.acq_time));
  const r1 = v => Math.round(v * 10) / 10, r4 = v => Math.round(v * 1e4) / 1e4;
  const cells = [...grid].map(([k, [c, sum, max]]) => { const [ix, iy] = k.split(',').map(Number); return [r4(ix * cell), r4(iy * cell), c, r1(sum), r1(max)]; });
  res.setHeader('Cache-Control', 's-maxage=1800');
  res.status(200).json({ rows, sampled: total > rows.length, total, source: 'NASA FIRMS', start: iso(start), end: iso(end), requests: jobs.length, warnings: warnings.slice(0, 10),
    grid: { cell, fields: ['west', 'south', 'count', 'frp_sum', 'frp_max'], cells } });
};
