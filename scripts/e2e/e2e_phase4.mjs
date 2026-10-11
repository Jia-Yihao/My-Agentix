/**
 * R4.6 阶段四端到端验收（统一入口 + 双模块 + 单一登录态）
 *
 * 运行方式（在 My-Agentix/web 目录）：
 *   node ../scripts/e2e_phase4.mjs
 * 前置：后端 sqlite demo :8000、vite dev :5173 均已启动。
 */
import { chromium } from 'file:///D:/Code/practice_project/Tracerag/frontend/node_modules/playwright-core/index.mjs'

const EXEC = 'C:/Users/15237/AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe'
const BASE = 'http://localhost:5173'
const OUT = 'D:/workbuddy工作目录/2026-09-21/agentix_phase4_e2e'

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`)
}

const browser = await chromium.launch({ executablePath: EXEC, headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })

try {
  // 1) 未登录访问统一入口 → 登录 gate
  await page.goto(BASE + '/', { waitUntil: 'networkidle' })
  const onLogin = await page.locator('.auth-form').isVisible().catch(() => false)
  check('01 未登录显示统一登录页（auth gate 生效）', onLogin)
  await page.screenshot({ path: `${OUT}/01-login-gate.png`, fullPage: true })

  // 2) 登录（默认登录 tab；e2e@agentix.cn 已由预置脚本注册）
  await page.locator('input[type=email]').fill('e2e@agentix.cn')
  await page.locator('input[type=password]').fill('e2e-pass-123')
  await page.locator('.auth-form button[type=submit]').click()
  await page.waitForSelector('.unified-shell', { timeout: 15000 })
  check('02 登录成功进入统一 shell（共享导航可见）', true)
  await page.waitForTimeout(1200)
  await page.screenshot({ path: `${OUT}/02-legal-module.png`, fullPage: true })

  // 3) 单一登录态：token 写入 localStorage
  const token = await page.evaluate(() => localStorage.getItem('agentix:access_token'))
  check('03 JWT token 持久化（agentix:access_token）', !!token && token.length > 20, token ? `len=${token.length}` : 'missing')

  // 4) 切换到知识库问答模块（共享导航，无二次登录）
  await page.locator('.nav-tab', { hasText: '知识库问答' }).click()
  await page.waitForURL('**/knowledge/home', { timeout: 10000 })
  await page.waitForTimeout(1500)
  const stillAuthed = await page.evaluate(() => !!localStorage.getItem('agentix:access_token'))
  const url = page.url()
  check('04 共享导航切换至 /knowledge/home（无重复登录）', url.includes('/knowledge/home') && stillAuthed, url)
  await page.screenshot({ path: `${OUT}/03-knowledge-home.png`, fullPage: true })

  // 5) 旧入口 URL 重定向（R4.5）
  await page.goto(BASE + '/kb', { waitUntil: 'networkidle' })
  const redirected = page.url().includes('/knowledge/kb')
  check('05 旧 URL /kb 重定向至 /knowledge/kb', redirected, page.url())
  await page.waitForTimeout(1000)
  await page.screenshot({ path: `${OUT}/04-knowledge-kb-redirect.png`, fullPage: true })

  // 6) 切回法律智能助手（登录态保持）
  await page.locator('.nav-tab', { hasText: '法律智能助手' }).click()
  await page.waitForTimeout(1200)
  const backLegal = new URL(page.url()).pathname === '/'
  const tokenKept = await page.evaluate(() => !!localStorage.getItem('agentix:access_token'))
  check('06 切回 legal 模块（登录态保持）', backLegal && tokenKept, page.url())
  await page.screenshot({ path: `${OUT}/05-legal-back.png`, fullPage: true })
} catch (err) {
  check('E2E 异常中断', false, String(err).slice(0, 300))
  await page.screenshot({ path: `${OUT}/99-error.png`, fullPage: true }).catch(() => {})
} finally {
  await browser.close()
}

const failed = results.filter((r) => !r.ok)
console.log(`\n===== SUMMARY: ${results.length - failed.length}/${results.length} passed =====`)
process.exit(failed.length ? 1 : 0)
