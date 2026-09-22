import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.VITE_LOOPFORGE_BACKEND || "http://127.0.0.1:8765";

export default defineConfig({
  base: "/",
  root: "frontend",
  plugins: [react()],
  server: {
    proxy: {
      "/api": backend,
      "/reports": backend,
    },
  },
  build: {
    outDir: "../web/console",
    emptyOutDir: true,
  },
});
