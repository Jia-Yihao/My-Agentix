/**
 * My Agentix 功能界面批量截图脚本。
 *
 * 运行（在 My-Agentix/web 或任意目录）：
 *   node shot_agentix.mjs
 * 前置：
 *   - 后端: .venv/Scripts/python -m uvicorn agentix.main:application --port 8000
 *     （本地演示用 DATABASE_URL=sqlite:///./data/agentix.db 覆盖 .env 的 MySQL）
 *   - 前端: cd web && npx vite --port 5173
 *   - NODE_PATH 指向含 playwright-core 的 node_modules（Tracerag/frontend）
 * 输出: D:/workbuddy工作目录/2026-09-20/agentix_screenshots/
 */
import fs from "node:fs";
import { chromium } from "file:///D:/Code/practice_project/Tracerag/frontend/node_modules/playwright-core/index.mjs";

const OUT = "D:/workbuddy工作目录/2026-09-20/agentix_screenshots";
fs.mkdirSync(OUT, { recursive: true });

const EXE = "C:/Users/15237/AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe";

const browser = await chromium.launch({
  executablePath: fs.existsSync(EXE) ? EXE : undefined,
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

async function shot(name, fullPage = false) {
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage });
  console.log("saved:", name);
}

// 1. 落地页（整页）
await page.goto("http://localhost:5173/", { waitUntil: "networkidle", timeout: 30000 }).catch(() => {});
await shot("01-landing-落地页", true);

// 2. 打开聊天窗口
const startBtn = page.locator("text=开始").first();
if (await startBtn.count()) {
  await startBtn.click().catch(() => {});
}
await page.waitForTimeout(1200);
await shot("02-chat-聊天窗口");

// 3. 快捷提问 chips 展示（若在 DOM 中）
const chips = page.locator("text=知识库来源");
if (await chips.count()) {
  await shot("03-chat-快捷提问与知识库入口");
}

// 4. 发送一条会触发知识库检索的问题（后端降级模式下仍返回兜底文案）
const input = page.locator("textarea, input[type='text']").last();
if (await input.count()) {
  await input.fill("知识库里的法律条文来源有哪些？");
  await input.press("Enter");
  await page.waitForTimeout(6000);
  await shot("04-chat-问答交互-降级兜底");
}

// 5. 上传面板状态（点击上传按钮展示 file input 触发区）
const uploadBtn = page.locator("text=上传文件").first();
if (await uploadBtn.count()) {
  await uploadBtn.click().catch(() => {});
  await page.waitForTimeout(500);
  await shot("05-chat-文件上传入口");
}

// 6. 健康检查 / readiness 的 API 响应留档（非 UI，佐证降级链路）
const ctx2 = await browser.newContext();
const p2 = await ctx2.newPage();
const health = await p2.goto("http://127.0.0.1:8000/health").then(r => r.text()).catch(e => String(e));
console.log("health:", String(health).slice(0, 200));
await ctx2.close();

await browser.close();
console.log("done");
