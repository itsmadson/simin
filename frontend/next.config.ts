import path from 'node:path';
import type { NextConfig } from 'next';

const config: NextConfig = {
  reactStrictMode: true,
  // Emit a self-contained server bundle with only the modules actually reached.
  // The runtime image copies .next/standalone and carries no node_modules, so
  // this is not an optimisation — without it the Docker build has nothing to
  // copy and fails outright.
  output: 'standalone',
  // Pin the trace root to this directory. Without it Next walks up and finds an
  // unrelated lockfile in the home directory, then traces the wrong tree.
  outputFileTracingRoot: path.join(__dirname),
  // The dashboard is a single-user tool served next to its API; there is no
  // image pipeline to configure and no remote content to optimise.
  images: { unoptimized: true },
  eslint: { ignoreDuringBuilds: true },
};

export default config;
