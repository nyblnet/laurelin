import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";

// Laurelin's web app builds to a fully self-contained bundle in ../static
// (which the API serves). No CDN, no external requests — everything is inlined
// so `pip install laurelin` ships a working UI with no Node toolchain required.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  build: {
    outDir: "../static",
    emptyOutDir: true,
    target: "es2020",
    chunkSizeWarningLimit: 4096,
  },
  server: {
    // `npm run dev` proxies API calls to a locally running `laurelin serve`.
    proxy: {
      "/api": "http://127.0.0.1:8787",
      "/health": "http://127.0.0.1:8787",
    },
  },
});
