import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  root: 'client',
  plugins: [react(), tailwindcss()],
  build: {
    outDir: '../cassandra_hoard/static',
    emptyOutDir: true,
  },
  server: {
    host: '127.0.0.1',
    port: Number(process.env.VITE_PORT || 5173),
    proxy: {
      '/api': `http://127.0.0.1:${process.env.CASSANDRA_PORT || process.env.PORT || 5190}`,
    },
  },
});
