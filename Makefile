.PHONY: install dev test lint format clean init-data run-server run-web docker-build docker-up docker-down help

# 安装依赖（开发：运行时 + 测试/工具链）
install:
	pip install -r requirements-dev.txt
	cd web && npm install

# 开发模式运行
dev:
	make -j 2 run-server run-web

# 运行服务端
run-server:
	python -m uvicorn agentix.main:application --reload --host 0.0.0.0 --port 8000

# 运行Web端
run-web:
	cd web && npm run dev

# 运行测试
test:
	pytest tests/ -v

# 代码检查（与 .github/workflows/ci.yml 的 lint job 同口径）
#
# ⚠️ 2026-09 修复：此前本目标的三步**都会失败**，且与 CI 口径不一致——
#   1) `flake8 agentix --max-line-length=100`：flake8 不是本仓工具链的一环
#      （CI 用 ruff），实测报 900 条（365×E501 / 347×W293，绝大多数是行太长
#      与行尾空白），必然非零退出。用 ruff 才是"同一把尺子"。
#   2) `black --check agentix`：在 CI 里这一步是 `continue-on-error: true`
#      —— 仅报告不阻断，因为仓库尚未全量 black 化（实测 332 文件待格式化，
#      见 ci.yml 该步骤上方的注释）。本地写成阻断目标 ⇒ 本地永远红、
#      CI 永远绿，两边对不上。Makefile 里用行首 `-` 表达"忽略本行退出码"，
#      正是 continue-on-error 的等价写法。
#   3) `mypy agentix`：**范围**比 CI 宽。CI 只对 domain 层负责
#      (`mypy agentix/domain`，配 pyproject 的 follow_imports="silent")，
#      扩到整个包会连带上报 infrastructure/interfaces 的存量问题（实测 67 条）
#      并把首次运行拉到 10–25 分钟。范围应与 CI 一致，而不是更宽。
#      另：`agentix/observability/metrics.py` 曾有一行散文注释误用 mypy 的
#      魔法前缀（`# type: ...`），使 mypy 报 invalid syntax 并终止**整次**
#      类型检查——即该目标此前**从未真正检查过任何类型**。BUG-19 已修，
#      并加了守卫测试 tests/test_p46_toolchain_parity.py 防止复发。
lint:
	ruff check agentix scripts evals kb_ingest
	-black --check --diff agentix scripts evals kb_ingest
	mypy agentix/domain --config-file=pyproject.toml

# 代码格式化
format:
	black agentix
	cd web && npm run lint

# 清理
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
	rm -rf dist/ build/ *.egg-info

# 初始化运行时数据目录
# 2026-10-06 完整性审查：原为 `mkdir -p data/chroma` —— chroma 早不是本项目
# 向量后端（R3.7 起为 Milvus legal 集合），该目录全仓无消费者，属死目标。
# 改为创建代码实际会写入的目录。
init-data:
	mkdir -p uploads data/tiktoken_cache data/lightrag data/indexes data/kb

# Docker构建
docker-build:
	docker-compose build

# Docker启动
docker-up:
	docker-compose up -d

# Docker停止
docker-down:
	docker-compose down

# 帮助
help:
	@echo "My Agentix - 开发命令"
	@echo ""
	@echo "make install       - 安装所有依赖"
	@echo "make dev           - 开发模式（同时运行服务端和Web端）"
	@echo "make run-server    - 运行服务端"
	@echo "make run-web       - 运行Web端"
	@echo "make test          - 运行测试"
	@echo "make lint          - 代码检查"
	@echo "make format        - 代码格式化"
	@echo "make clean         - 清理临时文件"
	@echo "make init-data     - 创建运行时数据目录"
	@echo "make docker-build  - Docker构建"
	@echo "make docker-up     - Docker启动"
