import { describe, expect, it } from "vitest";

import {
  EMPTY_RESPONSE_HINT,
  FALLBACK_CHAT_ERROR,
  decorateDegraded,
  emptyResponseMessage,
  extractChatError,
  friendlyErrorCode
} from "./chatContract";

describe("extractChatError", () => {
  it("AppError 形状优先取 user_hint", () => {
    const e = { response: { data: { error_code: "llm_unavailable", message: "模型不可用", user_hint: "请稍后重试" } } };
    expect(extractChatError(e)).toBe("请稍后重试");
  });

  it("无 user_hint 时取 message", () => {
    const e = { response: { data: { error_code: "x", message: "内部错误" } } };
    expect(extractChatError(e)).toBe("内部错误");
  });

  it("HTTPException detail 字符串", () => {
    const e = { response: { data: { detail: "Not Found" } } };
    expect(extractChatError(e)).toBe("Not Found");
  });

  it("HTTPException detail 对象拼 error_code", () => {
    const e = { response: { data: { detail: { error_code: "agent_timeout", message: "执行超时" } } } };
    expect(extractChatError(e)).toBe("执行超时（执行超时）");
  });

  it("422 校验数组取首条 msg", () => {
    const e = { response: { data: { detail: [{ msg: "field required" }] } } };
    expect(extractChatError(e)).toContain("field required");
  });

  it("APIKeyMiddleware { error, message } 形状", () => {
    const e = { response: { data: { error: "Unauthorized", message: "Invalid or missing API key" } } };
    expect(extractChatError(e)).toBe("Invalid or missing API key");
  });

  it("无响应体时退回网络错误信息", () => {
    expect(extractChatError(new Error("Network Error"))).toBe("Network Error");
  });

  it("彻底没有信息时用兜底文案", () => {
    expect(extractChatError(undefined)).toBe(FALLBACK_CHAT_ERROR);
  });
});

describe("decorateDegraded", () => {
  it("ok/缺省原样返回", () => {
    expect(decorateDegraded("hi", { status: "ok" })).toBe("hi");
    expect(decorateDegraded("hi", {})).toBe("hi");
    expect(decorateDegraded("hi", { status: null })).toBe("hi");
  });

  it("degraded 追加提示 + error_code 文案", () => {
    const out = decorateDegraded("答案", { status: "degraded", errorCode: "llm_unavailable" });
    expect(out.startsWith("答案")).toBe(true);
    expect(out).toContain("回答已降级");
    expect(out).toContain("模型服务暂不可用");
  });

  it("带 trace_id", () => {
    const out = decorateDegraded("答案", { status: "degraded", traceId: "abc123" });
    expect(out).toContain("abc123");
  });

  it("未知状态原样展示状态值", () => {
    expect(decorateDegraded("x", { status: "weird" })).toContain("响应状态：weird");
  });
});

describe("emptyResponseMessage / friendlyErrorCode", () => {
  it("空响应兜底可带 error_code", () => {
    expect(emptyResponseMessage()).toBe(EMPTY_RESPONSE_HINT);
    expect(emptyResponseMessage("no_evidence")).toContain("未找到足够证据");
  });

  it("未知码原样返回", () => {
    expect(friendlyErrorCode("brand_new_code")).toBe("brand_new_code");
    expect(friendlyErrorCode(null)).toBe("");
  });
});
