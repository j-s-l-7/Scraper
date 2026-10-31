import type { NextConfig } from "next";

// Static export served by FastAPI (console/api). In `--dev`, `next dev` proxies /api to the API on :8787.
const dev = process.env.NODE_ENV === "development";
const nextConfig: NextConfig = {
  ...(dev ? {} : { output: "export" as const }),
  trailingSlash: true,
  images: { unoptimized: true },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  ...(dev
    ? {
        async rewrites() {
          return [{ source: "/api/:path*", destination: "http://127.0.0.1:8787/api/:path*" }];
        },
      }
    : {}),
};

export default nextConfig;
