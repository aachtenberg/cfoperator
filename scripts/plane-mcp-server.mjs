#!/usr/bin/env node
/**
 * Wrapper around the upstream Plane MCP server.
 *
 * Upstream builds the GET comments URL without a trailing slash:
 *
 *     makePlaneRequest("GET",  `.../issues/${issue_id}/comments`)    <- 500
 *     makePlaneRequest("POST", `.../issues/${issue_id}/comments/`)   <- fine
 *
 * Plane answers the slashless path with a 500 rather than a redirect, so
 * get_issue_comments fails for every issue in every project. It is the only
 * request path in the package missing its slash, which is why every other
 * read tool works. Present in 0.1.5 too, and the npm TypeScript line has been
 * superseded by a Python rewrite, so no upstream fix is coming.
 *
 * That 500 is expensive rather than merely annoying: an agent that calls the
 * tool gets an opaque error, retries, and burns context before giving up.
 *
 * This installs the pinned upstream package into a cache directory, applies
 * the one-character fix, and runs it. Everything else is upstream's.
 *
 * Refs CFOP-156.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

/**
 * Pinned by commit 9e55c55: 0.1.5 hard-pins axios at 1.12.0, which sends
 * plain-HTTP through an egress proxy instead of CONNECT tunnels and gets 405'd
 * in sandboxed environments. 0.1.4's ^1.8.4 resolves to 1.19.0. The releases
 * otherwise differ by exactly one tool (0.1.5 adds list_project_issues), so
 * this pin is also why there is no project-wide list tool -- a deliberate
 * tradeoff, not an oversight. Bumping it means re-testing the proxy behaviour.
 */
const VERSION = "0.1.4";
const SPEC = `@makeplane/plane-mcp-server@${VERSION}`;

const CACHE = join(
  process.env.XDG_CACHE_HOME || join(homedir(), ".cache"),
  "cfoperator",
  "plane-mcp",
  VERSION,
);
const PKG = join(CACHE, "node_modules", "@makeplane", "plane-mcp-server");
const ENTRY = join(PKG, "build", "index.js");
const PATCH_TARGET = join(PKG, "build", "tools", "issues.js");

/**
 * Matched by content, never by line number: the statement sits at line 23 in
 * 0.1.4 and line 66 in 0.1.5, so a line-keyed patch would silently rewrite the
 * wrong statement. The POST path already ends "comments/`, {" and so cannot
 * collide with this pattern.
 */
const BROKEN = "/comments`);";
const FIXED = "/comments/`);";

/** stdout is the MCP channel. Diagnostics go to stderr, always. */
const log = (msg) => process.stderr.write(`[plane-mcp-wrapper] ${msg}\n`);

function install() {
  log(`installing ${SPEC} into ${CACHE}`);
  mkdirSync(CACHE, { recursive: true });
  // A package.json keeps npm from walking up and adopting a parent project.
  writeFileSync(
    join(CACHE, "package.json"),
    JSON.stringify(
      { name: "cfoperator-plane-mcp-wrapper", private: true, version: "1.0.0" },
      null,
      2,
    ) + "\n",
  );
  try {
    execFileSync(
      "npm",
      ["install", "--silent", "--no-audit", "--no-fund", "--prefix", CACHE, SPEC],
      // stdout to stderr: npm must never write to the MCP channel.
      { stdio: ["ignore", "inherit", "inherit"], env: { ...process.env } },
    );
  } catch (err) {
    log(`npm install failed: ${err.message}`);
    process.exit(1);
  }
}

/** Idempotent, and re-checked on every launch so a reinstall cannot un-fix it. */
function patch() {
  const src = readFileSync(PATCH_TARGET, "utf8");
  const hits = src.split(BROKEN).length - 1;

  if (hits === 0) {
    if (!src.includes(FIXED)) {
      // Upstream changed shape. Run unpatched rather than guess.
      log(`WARNING: neither the broken nor the fixed comments URL found in ` +
          `${PATCH_TARGET}. get_issue_comments may be broken. Running anyway.`);
    }
    return;
  }
  if (hits > 1) {
    log(`WARNING: expected 1 occurrence of the broken comments URL, found ` +
        `${hits}. Patching all of them.`);
  }
  writeFileSync(PATCH_TARGET, src.split(BROKEN).join(FIXED));
  log("patched get_issue_comments: restored the missing trailing slash");
}

if (!existsSync(ENTRY)) install();
if (!existsSync(ENTRY)) {
  log(`upstream entry point missing after install: ${ENTRY}`);
  process.exit(1);
}
patch();

await import(pathToFileURL(ENTRY).href);
