SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

.PHONY: test test-v test-install test-lock-regen dev stop logs restart

# --- Tests ---

SDIST_ONLY := fnvhash,mock-open,paho-mqtt,pyric

test-install:
	awk '/^setuptools==/{f=1} f && $$0 !~ /^setuptools==/ && /^[A-Za-z0-9_.-]+==/{exit} f{print} \
		END{if (!f) { print "setuptools not found in requirements-test.lock" > "/dev/stderr"; exit 1 }}' \
		requirements-test.lock | pip install --require-hashes -r /dev/stdin
	pip install --require-hashes --no-build-isolation --only-binary :all: --no-binary $(SDIST_ONLY) -r requirements-test.lock

test-lock-regen:
	pip install -q uv==0.5.8
	uv pip compile --universal --python-version 3.12 --generate-hashes \
		--custom-compile-command "make test-lock-regen" \
		requirements-test.txt -o requirements-test.lock

test:
	python3 -m pytest tests/ -q

test-v:
	python3 -m pytest tests/ -v

lint-dup:
	python3 -m pylint --disable=all --enable=duplicate-code \
		--min-similarity-lines=10 \
		tests custom_components

# --- Dev environment ---

dev:
	docker compose -f docker-compose.dev.yml up -d
	python3 scripts/dev-setup.py

stop:
	docker compose -f docker-compose.dev.yml down

logs:
	docker logs ha-exo-pool-dev -f --tail 50

restart:
	docker restart ha-exo-pool-dev
