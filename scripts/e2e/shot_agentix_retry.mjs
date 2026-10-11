/** 补拍：等待降级兜底回复出现后的聊天窗口终态。运行方式同 shot_agentix.mjs */
import { chromium } from "file:///D:/Code/practice_project/Tracerag/frontend/node_modules/playwright-core/index.mjs";

const OUT = "D:/workbuddy工作目录/2026-09-20/agentix_screenshots";
const EXE = "C:/Users/15237/AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe";
import fs from "node:fs";

const browser = await chromium.launch({ executablePath: fs.existsSync(EXE) ? EXE : undefined, headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto("http://localhost:5173/", { waitUntil: "networkidle", timeout: 30000 }).catch(() => {});
const startBtn = page.locator("text=开始").first();
if (await startBtn.count()) await startBtn.click().catch(() => {});
await page.waitForTimeout(1000);
const input = page.locator("textarea, input[type='text']").last();
await input.fill("你好，请介绍一下你能回答哪些法律问题");
await input.press("Enter");
await page.waitForTimeout(25000); // 等待 LLM 超时 → 兜底文案
await page.screenshot({ path: `${OUT}/04-chat-问答交互-降级兜底.png` });
console.log("re-shot done");
await browser.close();
