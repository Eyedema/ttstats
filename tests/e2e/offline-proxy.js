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
 *
 * Point a page at `proxy.origin` instead of the baseURL. Cookies are per host,
 * not per port, so the saved session applies; the worker registers afresh
 * for the proxy's origin, so its caches start empty in every test.
 */
async function startOfflineProxy(targetURL) {
  const target = new URL(targetURL);
  let mode = 'online';
  const sockets = new Set();
  const hung = new Set();

  const server = http.createServer((req, res) => {
    if (mode === 'offline') {
      req.socket.destroy();
      return;
    }
    if (mode === 'hang') {
      hung.add(res);
      return;
    }
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
        res.writeHead(answer.statusCode, answer.headers);
        answer.pipe(res);
      }
    );
    upstream.on('error', () => res.destroy());
    req.pipe(upstream);
  });

  server.on('connection', (socket) => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
  });

  await new Promise((resolve) => server.listen(0, 'localhost', resolve));

  return {
    origin: `http://localhost:${server.address().port}`,
    setMode(next) {
      mode = next;
      if (next === 'offline') {
        for (const socket of sockets) socket.destroy();
      }
      if (next === 'online') {
        for (const res of hung) res.destroy();
        hung.clear();
      }
    },
    async close() {
      for (const res of hung) res.destroy();
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

module.exports = { startOfflineProxy };
