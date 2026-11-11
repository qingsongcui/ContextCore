import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    // The installed service may serve workspace dist directly. Ordinary builds
    // must remain isolated and portable. Use --outDir for an explicit target.
    outDir: 'dist-staging',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('highlight.js') || id.includes('lowlight') || id.includes('rehype-highlight')) {
            return 'markdown-highlight';
          }
          return undefined;
        },
      },
    },
  },
  server: {
    port: 5173,
    host: '127.0.0.1',
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8420', changeOrigin: false },
      '/health': { target: 'http://127.0.0.1:8420', changeOrigin: false },
      '/mcp': { target: 'http://127.0.0.1:8420', changeOrigin: false },
    },
  },
});
