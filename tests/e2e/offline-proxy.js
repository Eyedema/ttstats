const http = require('http');

/**
 * A switchable network between the browser and the e2e server (KAN-82).
 *
 * Why not context.setOffline(): in WebKit it fails a controlled page's
 * requests before the service worker sees them -- even a fetch the worker
 * would answer from its cache fails with "Load failed", and a reload is
 * "WebKit encountered an internal error". So setOffline() can never show a
 * worker serving anything offline in WebKit, and a spec asserting "not
 * served from cache" with it passes whatever the worker does.
 *
 * This proxy fails at the network instead, which is what a dead zone is:
 *
 *   online   forward to the server
 *   offline  refuse: every open connection and new request is destroyed
 *   hang     accept and never answer (lie-fi)
 *   502      answer every request 502 Bad Gateway (the server is down
 *            behind a working reverse proxy; KAN-133)
 *
 * `release()` goes back online and FORWARDS the requests `hang` was holding,
 * so their answers arrive late (a network that recovers); setMode('online')
 * drops them instead.
 *
 * Options: `{ csp }` adds that Content-Security-Policy to every text/html
 * answer -- the production policy, for a spec where a live service worker
 * keeps applyProdCSP()'s page.route() from ever seeing the request.
 *
 * `requests` logs every request that reached the proxy, whatever the mode
 * (`{method, path, mode}`): "no request went out" is asserted against it.
 *
 * Point a page at `proxy.origin` instead of the baseURL. Cookies are per host,
 * not per port, so the saved session applies; the worker registers afresh
 * for the proxy's origin, so its caches start empty in every test.
 */
async function startOfflineProxy(targetURL, { csp } = {}) {
  const target = new URL(targetURL);
  let mode = 'online';
  const sockets = new Set();
  const hung = new Map();
  const requests = [];

  function forward(req, res) {
    // Host is passed through unchanged: Django checks ALLOWED_HOSTS without
    // the port, and CSRF compares Origin with this same host:port.
    const upstream = http.request(
      {
        host: target.hostname,
        port: target.port,
        path: req.url,
        method: req.method,
        headers: req.headers,
      },
      (answer) => {
        const headers = { ...answer.headers };
        if (csp && String(headers['content-type'] || '').startsWith('text/html')) {
          headers['content-security-policy'] = csp;
        }
        res.writeHead(answer.statusCode, headers);
        answer.pipe(res);
      }
    );
    upstream.on('error', () => res.destroy());
    req.pipe(upstream);
  }

  const server = http.createServer((req, res) => {
    requests.push({ method: req.method, path: req.url, mode });
    if (mode === 'offline') {
      req.socket.destroy();
      return;
    }
    if (mode === 'hang') {
      hung.set(res, req);
      return;
    }
    if (mode === '502') {
      req.resume();
      res.writeHead(502, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end('<!DOCTYPE html><title>502 Bad Gateway</title><h1>502 Bad Gateway</h1>');
      return;
    }
    forward(req, res);
  });

  server.on('connection', (socket) => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
  });

  await new Promise((resolve) => server.listen(0, 'localhost', resolve));

  return {
    origin: `http://localhost:${server.address().port}`,
    requests,
    setMode(next) {
      mode = next;
      if (next === 'offline') {
        for (const socket of sockets) socket.destroy();
      }
      if (next === 'online') {
        for (const res of hung.keys()) res.destroy();
        hung.clear();
      }
    },
    /** Back online, answering the held requests now (late). */
    release() {
      mode = 'online';
      const held = [...hung.entries()];
      hung.clear();
      for (const [res, req] of held) forward(req, res);
    },
    async close() {
      for (const res of hung.keys()) res.destroy();
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

module.exports = { startOfflineProxy };
