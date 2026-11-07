---
name: release-checklist
description: 提交/发版前跑完整本地验证（ruff、守卫、全量 pytest、前端检查）的顺序与命令
anti_when: 只改了 docs 文档时（跑守卫 + ruff 即可，全量 pytest 7-8 分钟不必）；或用户明确只要快速冒烟时
---
# 发布前验证清单

按顺序执行；任一步失败先修复再继续（不要跳步）。

1. **Ruff 主检**（源码 + 严格 F 码）：

   ```powershell
   & ".\.venv\python.exe" -m ruff check agentix scripts evals kb_ingest tests
   ```

2. **结构与卫生检查**：

   ```powershell
   & ".\.venv\python.exe" -m pytest tests/test_p36_structure_hygiene.py -q
   ```

3. **全量后端**（约 7-8 分钟，mypy 同批跑）：

   ```powershell
   & ".\.venv\python.exe" -m pytest tests/ -m "not integration" -q
   & ".\.venv\python.exe" -m mypy agentix
   ```

4. **前端**（改过 web/ 时）：

   ```powershell
   Set-Location web
   npm run lint
   npm run test
   npm run build
   Set-Location ..
   ```

5. **基线数字**（若通过数变化）：同步 README.md 与 CHANGELOG。

注意：命令行环境没有 heredoc，批量替换用临时 .py 脚本。
新增文件不要带 BOM。
