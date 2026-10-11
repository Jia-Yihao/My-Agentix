import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";

// R4.4：单一构建链（Vue3 + Vite 8 + vue-tsc 门禁），双模块共用。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backend = env.VITE_API_BASE_URL || "http://localhost:8000";
  return {
    plugins: [vue()],
    server: {
      port: Number(env.VITE_PORT || 5173),
      host: true,
      proxy: {
        "/api": { target: backend, changeOrigin: true },
        "/uploads": { target: backend, changeOrigin: true },
        // 后端只有 /health（main.py；无 /healthz——旧代理前缀会 404）
        "/health": { target: backend, changeOrigin: true },
      },
    },
    build: {
      outDir: "dist",
    },
  };
});
