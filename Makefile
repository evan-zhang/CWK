.PHONY: doctor test aodw-check ci smoke smoke-ai smoke-ai-degraded wiki-lint wiki-smoke clean

PYTHON ?= python3
TEST_TMPDIR ?= $(shell $(PYTHON) -c 'import os,tempfile; print(os.path.realpath(tempfile.gettempdir()))')
SMOKE_RUN ?= ci-smoke
SMOKE_DATE ?= 2026-01-01
SMOKE_AI_RUN ?= ci-smoke-ai
SMOKE_AI_DEGRADED_RUN ?= ci-smoke-ai-degraded

doctor:
	$(PYTHON) scripts/cwk_doctor.py --check-only --config skill/templates/CONFIG.example.json

test:
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

# CI 与本地共用的唯一入口。CI 跑什么本地就跑什么，反过来也一样——
# 两边命令一旦不同，「CI 是绿的」这句话就不再是本地可复现的证据。
ci:
	$(MAKE) test
	$(MAKE) aodw-check

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
