/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The Viewer is read-only: it never posts, and it never embeds remote assets.
  poweredByHeader: false,
  /*
   * Pin the workspace root to this directory.
   *
   * Next infers the root by walking up for lockfiles, and an unrelated
   * package-lock.json further up the tree (for example in the home directory)
   * makes it choose that instead. The Viewer is self-contained, so its own
   * directory is the correct tracing root.
   */
  outputFileTracingRoot: import.meta.dirname,
  /*
   * The dev-tools badge is pinned to the bottom-left corner, which is exactly
   * where the rail keeps its display controls. It is a development-only
   * overlay, so turning it off costs nothing and keeps the rail clickable.
   */
  devIndicators: false,
};

export default nextConfig;
