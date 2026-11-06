#!/usr/bin/env node
/**
 * Size budget of the MCP Apps report viewer (task 3). Hosts embed the View as
 * one HTML resource; these ceilings keep it small enough to load inline. They
 * are this task's budget, not a host limit: real host ceilings are measured in
 * the delivery acceptance and may only lower them.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import zlib from "node:zlib";

const RAW_BUDGET_BYTES = 1_100_000;
const GZIP_BUDGET_BYTES = 330_000;

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const file = path.join(root, "dist", "report-viewer", "report-viewer.html");
const html = fs.readFileSync(file);
const raw = html.length;
const gzip = zlib.gzipSync(html, { level: 9 }).length;
console.log(`[report-viewer] ${path.relative(root, file)}: ${raw} bytes, ${gzip} bytes gzip`);
const problems = [];
if (raw > RAW_BUDGET_BYTES) problems.push(`raw size ${raw} exceeds ${RAW_BUDGET_BYTES}`);
if (gzip > GZIP_BUDGET_BYTES) problems.push(`gzip size ${gzip} exceeds ${GZIP_BUDGET_BYTES}`);
if (!html.includes('id="fs-third-party-notices"')) problems.push("third-party notices are missing");
if (/<script[^>]*\ssrc=|<link[^>]*rel="stylesheet"/i.test(html.toString("utf-8"))) {
  problems.push("the viewer references an external script or stylesheet");
}
if (problems.length) {
  for (const problem of problems) console.error(`[report-viewer] ${problem}`);
  process.exit(1);
}
