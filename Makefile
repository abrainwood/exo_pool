.PHONY: test test-v test-install test-lock-regen dev dev-stop dev-logs dev-restart

# --- Tests ---

test-install:
	pip install --require-hashes --no-deps -r requirements-test-sdist.lock
	pip install --require-hashes --only-binary :all: -r requirements-test.lock

test-lock-regen:
	pip install -q uv
	python3 scripts/regen_test_lock.py

test:
	python3 -m pytest tests/ -q

test-v:
	python3 -m pytest tests/ -v

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
