PYTHON ?= python3

.PHONY: all test verify

all: verify

test:
	@set -e; \
	for test_file in tests/test_*.py; do \
		$(PYTHON) "$$test_file"; \
	done

verify: test
	$(PYTHON) tools/verify_repo.py
