import assert from "node:assert/strict";
import test from "node:test";

import { parseApiResponse } from "../src/api-response.mjs";

function response({ status = 200, body = "", contentType = "application/json" } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get(name) {
        return name.toLowerCase() === "content-type" ? contentType : null;
      },
    },
    async text() {
      return body;
    },
  };
}

test("后端不可用且代理返回空响应时给出可操作错误", async () => {
  await assert.rejects(
    parseApiResponse(response({ status: 502 })),
    /HTTP 502.*后端已启动/,
  );
});

test("成功响应为空时不暴露 JSON 解析异常", async () => {
  await assert.rejects(parseApiResponse(response()), /服务返回空响应/);
});

test("错误响应不是 JSON 时保留 HTTP 状态并提示后端", async () => {
  await assert.rejects(
    parseApiResponse(response({ status: 503, body: "Service Unavailable", contentType: "text/plain" })),
    /HTTP 503.*后端已启动/,
  );
});

test("JSON 错误沿用后端摘要，正常 JSON 原样返回", async () => {
  await assert.rejects(
    parseApiResponse(response({ status: 401, body: JSON.stringify({ summary: "Token 无效" }) })),
    /Token 无效/,
  );

  const payload = { status: "completed", tasks: [] };
  assert.deepEqual(
    await parseApiResponse(response({ body: JSON.stringify(payload) })),
    payload,
  );
});
