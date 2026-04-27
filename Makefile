.PHONY: test dev dev-stop dev-logs dev-restart

# --- Tests ---

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
