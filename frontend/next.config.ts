import path from 'node:path';
import type { NextConfig } from 'next';

const config: NextConfig = {
  reactStrictMode: true,
  // Pin the trace root to this directory. Without it Next walks up and finds an
  // unrelated lockfile in the home directory, then traces the wrong tree.
  outputFileTracingRoot: path.join(__dirname),
  // The dashboard is a single-user tool served next to its API; there is no
  // image pipeline to configure and no remote content to optimise.
  images: { unoptimized: true },
  eslint: { ignoreDuringBuilds: true },
};

export default config;
