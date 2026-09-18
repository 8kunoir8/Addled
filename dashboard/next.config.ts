import type { NextConfig } from "next";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * The app version is maintained in the Electron package.json one level up — the
 * single place it exists. Reading it at build time means the About tab and the
 * Guide cannot drift from the version that was actually built; they previously
 * claimed "1.0.0" against a real 1.0.16. If the file cannot be read the value
 * becomes "unknown", which is honest, rather than a stale number.
 */
function readAppVersion(): string {
  const candidates = [
    join(process.cwd(), "..", "package.json"),
    join(process.cwd(), "package.json"),
  ];
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(readFileSync(candidate, "utf8"));
      if (typeof parsed?.version === "string" && parsed.version) {
        return parsed.version;
      }
    } catch {
      // try the next candidate
    }
  }
  return "unknown";
}

const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
  env: { NEXT_PUBLIC_ADDED_VERSION: readAppVersion() },
};

export default nextConfig;