// Vercel serverless function: GET /api/status → tells the website whether real FIRMS data is available.
module.exports = (req, res) => {
  res.status(200).json({ live: Boolean((process.env.FIRMS_MAP_KEY || '').trim()), weather: Boolean((process.env.OPENWEATHER_KEY || '').trim()), today: new Date().toISOString().slice(0, 10) });
};
