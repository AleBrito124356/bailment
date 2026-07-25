/**
 * The dashboard is a static-ish client application that talks to the broker over CORS.
 *
 * There is deliberately no rewrite proxying `/api` to the broker. A proxy would mean the
 * dashboard's own origin could reach the broker without a token attached by the browser,
 * and the moment that exists somebody puts a service token in the Next process so the
 * pages "just work" -- at which point every visitor to the dashboard holds operator
 * rights. The browser sends the token, the broker checks it, and the dashboard has no
 * credential of its own to leak.
 *
 * @type {import('next').NextConfig}
 */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
};

export default nextConfig;
