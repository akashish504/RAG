.PHONY: install dev lint test run-pipeline create-index run-airtable-ingestion discover-airtable-schema docker-build docker-shell docker-test docker-lint docker-read-s3 docker-run-pipeline docker-run-airtable-ingestion docker-discover-airtable-schema docker-run-poller reset-deployment clean

install:
	pip install -e .

dev:
	pip install -e ".[dev,api]"

lint:
	ruff check src tests
	mypy src

test:
	pytest

run-pipeline:
	python scripts/run_pipeline.py

create-index:
	python scripts/create_opensearch_index.py

run-airtable-ingestion:
	python scripts/run_airtable_ingestion.py --target profiles_sync

discover-airtable-schema:
	python scripts/discover_airtable_schema.py

docker-build:
	docker compose build

docker-shell:
	docker compose --profile tools run --rm shell

docker-test:
	docker compose --profile tools run --rm test

docker-lint:
	docker compose --profile tools run --rm lint

docker-read-s3:
	docker compose --profile tools run --rm s3-smoke

docker-run-pipeline:
	docker compose run --rm pipeline python scripts/run_pipeline.py --prefix $${S3_PREFIX:-raw/}

docker-run-airtable-ingestion:
	docker compose run --rm pipeline python scripts/run_airtable_ingestion.py --target $${AIRTABLE_INGEST_TARGET:-profiles_sync}

docker-discover-airtable-schema:
	docker compose run --rm pipeline python scripts/discover_airtable_schema.py

docker-run-poller:
	docker compose run --rm pipeline python scripts/run_poller.py $${POLLER_ARGS:---dry-run}

reset-deployment:
	bash scripts/reset_deployment.sh

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
