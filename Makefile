.PHONY: doctor test smoke smoke-ai smoke-ai-degraded wiki-lint wiki-smoke clean

PYTHON ?= python3
SMOKE_RUN ?= ci-smoke
SMOKE_DATE ?= 2026-01-01
SMOKE_AI_RUN ?= ci-smoke-ai
SMOKE_AI_DEGRADED_RUN ?= ci-smoke-ai-degraded

doctor:
	$(PYTHON) scripts/cwk_doctor.py --check-only --config skill/templates/CONFIG.example.json

test:
	$(MAKE) doctor
	$(PYTHON) -m py_compile scripts/*.py
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'
	$(MAKE) smoke
	$(MAKE) smoke-ai
	$(MAKE) smoke-ai-degraded

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
