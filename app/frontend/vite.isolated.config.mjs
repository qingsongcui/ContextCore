import { mergeConfig } from 'vite';
import config from './vite.config.ts';

// Explicit isolated development endpoint; never targets the installed service.
export default mergeConfig(config, {
  define: { 'import.meta.env.VITE_ISOLATED_PREVIEW': 'true' },
  server: {
    port: 15173,
    strictPort: true,
    proxy: Object.fromEntries(['/api', '/health', '/mcp'].map(path => [path, {
      target: 'http://127.0.0.1:18420', changeOrigin: false,
    }])),
  },
});
