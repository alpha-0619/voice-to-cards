import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // 5173/5174 and 8000/8001 are in use by other projects on this machine.
    port: 5175,
    strictPort: true,
    // Proxy rather than CORS in development, so the browser sees one origin
    // and the streaming response is not re-buffered by a preflight round trip.
    proxy: {
      "/api": { target: "http://127.0.0.1:8010", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8010", changeOrigin: true },
    },
  },
  build: {
    // Built into the repo's public/ directory, which Vercel serves from the
    // CDN. The same directory is served by FastAPI when running locally, so
    // `uvicorn app.main:app` alone reproduces the production shape on one port.
    outDir: "../public",
    emptyOutDir: true,
    // No vendor chunking games. The point of building the components by hand
    // instead of pulling in a component library is that the bundle stays small
    // enough not to need them.
    reportCompressedSize: true,
  },
});
