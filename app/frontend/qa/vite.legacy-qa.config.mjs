import { mergeConfig } from 'vite';
import isolated from '../vite.isolated.config.mjs';

// Explicit test fixture: emulate an old backend for the compatibility probe.
// All other API traffic stays on the isolated development service.
export default mergeConfig(isolated, {
  server: { port: 15174 },
  plugins: [{
    name: 'legacy-backend-qa-only',
    configureServer(server) {
      const requests = [];
      server.middlewares.use((req, res, next) => {
        if (req.url?.startsWith('/api/')) requests.push({ method: req.method, path: req.url.split('?')[0] });
        if (req.url === '/__qa__/requests') {
          res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(requests)); return;
        }
        if (req.url?.startsWith('/api/manage/status')) {
          res.statusCode = 404; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ detail: 'Isolated legacy compatibility fixture' })); return;
        }
        next();
      });
    },
  }],
});
