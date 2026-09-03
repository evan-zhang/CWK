.PHONY: doctor test test-full aodw-check governance-audit ci ci-full smoke smoke-ai smoke-ai-degraded wiki-lint wiki-smoke clean

PYTHON ?= python3
TEST_TMPDIR ?= $(shell $(PYTHON) -c 'import os,tempfile; print(os.path.realpath(tempfile.gettempdir()))')
SMOKE_RUN ?= ci-smoke
SMOKE_DATE ?= 2026-01-01
SMOKE_AI_RUN ?= ci-smoke-ai
SMOKE_AI_DEGRADED_RUN ?= ci-smoke-ai-degraded

doctor:
	$(PYTHON) scripts/cwk_doctor.py --check-only --config skill/templates/CONFIG.example.json

# 车道两层（RT-038，实测见 RT/RT-038/rt-lite.md）：
# - `test` / `ci` 是快车道：单测排除 PR-001 安全族（test_pr001_*.py，本地实测
#   合计约 70+ 分钟，占全量时长绝大头），保留其余全部单测 + 三条脱敏 smoke，
#   本地约 3 分钟。日常迭代、文档、回执类改动用它。
# - `test-full` / `ci-full` 是全量车道：全部单测。发布或产品代码改动前必须
#   本地过一次；ci.yml 的每夜 schedule 也会跑一遍兜底。
# - 旧 test-lite/ci-lite 已退役：它只排除 test_pr001_release_gate_validation.py，
#   但 PR-001 其余 10 个测试文件同样是时长主体，「轻量」车道并没有轻多少。
# - macOS 本地注意：仓库若放在 /tmp 下，/tmp→/private/tmp 符号链接会让 VGA
#   的实例根链检查 fail-closed（cwk_instance.InstanceRootError）；从
#   /private/tmp/... 路径进入工作目录即可。另外默认 TMPDIR 路径较长，会
#   触发 rt032 socket 夹具的 AF_UNIX 104 字符上限，加 TEST_TMPDIR=/private/tmp。
#   本地建议跑法：cd /private/tmp/<repo> && env -u CWORK_APP_KEY make test TEST_TMPDIR=/private/tmp
# - governance-audit 的 CC-2 断言要求 ci 的 recipe 含 governance-audit，别摘。
test:
	$(MAKE) doctor
	$(PYTHON) -m py_compile scripts/*.py
	cd tests && TMPDIR="$(TEST_TMPDIR)" $(PYTHON) -m unittest $(shell cd tests && find . -maxdepth 1 -name 'test_*.py' ! -name 'test_pr001_*.py' -exec basename {} .py \; | sort | tr '\n' ' ')
	$(MAKE) smoke
	$(MAKE) smoke-ai
	$(MAKE) smoke-ai-degraded

test-full:
	$(MAKE) doctor
	$(PYTHON) -m py_compile scripts/*.py
	TMPDIR="$(TEST_TMPDIR)" $(PYTHON) -m unittest discover -s tests -p 'test_*.py'
	$(MAKE) smoke
	$(MAKE) smoke-ai
	$(MAKE) smoke-ai-degraded

# 方法层自检：AODW 框架 fixture + 受管 RT 门禁 + RT 花名册一致性。
# 判据和作用域都写在 .aodw-next/ 里，这里只留一个稳定入口。
aodw-check:
	bash .aodw-next/06-project/aodw-check.sh --root .

# 代码层自检：当前代码树上每个受跟踪文件归谁管、怎么改（RT-030 建立）。
# 与 aodw-check 分工——aodw-check 管方法层（RT 流程本身），本目标管产品代码归属。
# 判据面是 `git ls-files` 全集，不是「新增文件才受管」。
governance-audit:
	$(PYTHON) .aodw-next/06-project/governance-audit.py --root .

# CI 与本地共用的两个入口：ci（快车道，push/PR）与 ci-full（全量，每夜 schedule）。
# 两条车道都由 .github/workflows/ci.yml 调用；CI 跑什么本地就跑什么，
# 反过来也一样——两边命令一旦不同，「CI 是绿的」这句话就不再是本地可
# 复现的证据。governance-audit 的 CC-2 断言 ci 含 governance-audit。
ci:
	$(MAKE) test
	$(MAKE) aodw-check
	$(MAKE) governance-audit

ci-full:
	$(MAKE) test-full
	$(MAKE) aodw-check
	$(MAKE) governance-audit

wiki-lint:
	$(PYTHON) scripts/cwk_wiki_query.py --lint

wiki-smoke:
	$(PYTHON) scripts/cwk_wiki_smoke_test.py

smoke:
	rm -rf runs/$(SMOKE_RUN)
	$(PYTHON) scripts/cwk_nightly_pipeline.py \
		--config skill/templates/CONFIG.example.json \
		--run-name $(SMOKE_RUN) \
		--date $(SMOKE_DATE) \
		--source-dir tests/smoke/raw \
		--no-publish-mirror
	test -f runs/$(SMOKE_RUN)/digest-human-v4.md
	test -f runs/$(SMOKE_RUN)/digest-human-v4.html
	test -f runs/$(SMOKE_RUN)/action-cards.json
	test -f runs/$(SMOKE_RUN)/action-center.md
	test -f runs/$(SMOKE_RUN)/action-center.html
	test -f runs/$(SMOKE_RUN)/nightly-pipeline-manifest.json

smoke-ai:
	rm -rf runs/$(SMOKE_AI_RUN)
	CWK_AI_ENABLED=true CWK_AI_DRY_RUN=true $(PYTHON) scripts/cwk_nightly_pipeline.py \
		--config skill/templates/CONFIG.example.json \
		--run-name $(SMOKE_AI_RUN) \
		--date $(SMOKE_DATE) \
		--source-dir tests/smoke/raw \
		--no-publish-mirror
	test -f runs/$(SMOKE_AI_RUN)/digest-human-v4.md
	test -f runs/$(SMOKE_AI_RUN)/digest-ai-enhanced.md
	test -f runs/$(SMOKE_AI_RUN)/digest-ai-enhanced.html
	test -f runs/$(SMOKE_AI_RUN)/quality-review.json
	test -f runs/$(SMOKE_AI_RUN)/quality-review.md
	test -f runs/$(SMOKE_AI_RUN)/action-center.html
	grep -q '"degraded": false' runs/$(SMOKE_AI_RUN)/nightly-pipeline-manifest.json

smoke-ai-degraded:
	rm -rf runs/$(SMOKE_AI_DEGRADED_RUN)
	CWK_AI_ENABLED=true CWK_AI_DRY_RUN=false \
	CWK_AI_CALL_RETRIES=1 CWK_AI_TIMEOUT_SECONDS=1 \
	CWK_AI_RECORD_MODEL= CWK_AI_CLUSTER_MODEL= CWK_AI_QUALITY_MODEL= \
	$(PYTHON) scripts/cwk_nightly_pipeline.py \
		--config skill/templates/CONFIG.example.json \
		--run-name $(SMOKE_AI_DEGRADED_RUN) \
		--date $(SMOKE_DATE) \
		--source-dir tests/smoke/raw \
		--no-publish-mirror
	test -f runs/$(SMOKE_AI_DEGRADED_RUN)/digest-human-v4.md
	test -f runs/$(SMOKE_AI_DEGRADED_RUN)/action-center.html
	grep -q '"degraded": true' runs/$(SMOKE_AI_DEGRADED_RUN)/nightly-pipeline-manifest.json

clean:
	rm -rf runs/ci-smoke runs/ci-smoke-ai runs/ci-smoke-ai-degraded runs/rt001-smoke runs/local-readme-smoke runs/clone-smoke-* __pycache__ scripts/__pycache__ tests/__pycache__
