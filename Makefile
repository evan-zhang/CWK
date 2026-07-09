.PHONY: test smoke clean

PYTHON ?= python3
SMOKE_RUN ?= ci-smoke
SMOKE_DATE ?= 2026-01-01

test:
	$(PYTHON) -m py_compile scripts/*.py
	$(MAKE) smoke

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
	test -f runs/$(SMOKE_RUN)/nightly-pipeline-manifest.json

clean:
	rm -rf runs/ci-smoke runs/local-readme-smoke runs/clone-smoke-* __pycache__ scripts/__pycache__
