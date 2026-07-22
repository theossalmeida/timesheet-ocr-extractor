/** @type {import('next').NextConfig} */
const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8000";

const nextConfig = {
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
