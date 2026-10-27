const https = require('https'), http = require('http'), fs = require('fs');
https.createServer({ key: fs.readFileSync(process.env.TLS_DIR + '/k.pem'), cert: fs.readFileSync(process.env.TLS_DIR + '/c.pem') }, (req, res) => {
  const h = Object.assign({}, req.headers, { 'x-forwarded-proto': 'https' });
  const p = http.request({ host: '127.0.0.1', port: 18099, method: req.method, path: req.url, headers: h }, (r) => { res.writeHead(r.statusCode, r.headers); r.pipe(res); });
  p.on('error', () => { res.writeHead(502); res.end(); }); req.pipe(p);
}).listen(18443);
