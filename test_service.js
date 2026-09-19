"use strict";

const { spawnSync } = require("node:child_process");

// 基础契约 + 领域规则 + HTTP 接口 + 并发一致性，统一由 unittest 发现。
const result = spawnSync(
  "python3",
  ["-m", "unittest", "-v",
   "service_contract", "test_domain", "test_http_api", "test_concurrency"],
  { stdio: "inherit" },
);

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
