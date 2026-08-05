/** @type {import('next').NextConfig} */
const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8000";

// Comma-separated list of extra origins allowed to hit the dev server
// (e.g. your tunnel's public URL, without protocol: "abc123.trycloudflare.com").
const allowedDevOrigins = process.env.ALLOWED_DEV_ORIGINS
  ? process.env.ALLOWED_DEV_ORIGINS.split(",").map((o) => o.trim()).filter(Boolean)
  : undefined;

const nextConfig = {
  allowedDevOrigins,
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${backendUrl}/:path*`,
      },
      {
        source: "/health",
        destination: `${backendUrl}/health`,
      },
      {
        source: "/extract/:path*",
        destination: `${backendUrl}/extract/:path*`,
      },
      {
        source: "/contracheque/:path*",
        destination: `${backendUrl}/contracheque/:path*`,
      },
      {
        source: "/preview",
        destination: `${backendUrl}/preview`,
      },
    ];
  },
};

export default nextConfig;
