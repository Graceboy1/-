import type { NextConfig } from "next";

const apiPaths = [
  "health", "cases", "analyze", "replay",
  "market/status", "market/stocks", "market/stocks/:code",
  "paper/dashboard", "paper/start", "paper/stop", "paper/performance", "paper/playback",
  "news/feed", "news/status", "news/refresh", "news/analyze",
  "research/start", "research/records", "research/records/:record_id",
  "workspace/status", "workspace/setup", "workspace/register", "workspace/login",
  "workspace/model", "workspace/offline", "workspace/logout",
];

const config: NextConfig = {
  distDir: process.env.CONTEST_WEB_BUILD_DIR === ".next-seat" ? ".next-seat" : ".next",
  devIndicators: false,
  poweredByHeader: false,
  async rewrites() {
    return apiPaths.map((path) => ({
      source: `/contest-api/${path}`,
      destination: `http://127.0.0.1:8001/${path}`,
    }));
  },
};

export default config;
