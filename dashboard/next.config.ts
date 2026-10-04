import type { NextConfig } from "next";

const config: NextConfig = {
  // A self-contained server bundle for the container image: no node_modules in the runtime layer.
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
};

export default config;
