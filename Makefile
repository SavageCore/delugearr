.PHONY: install-hooks lint test deploy

install-hooks:
	lefthook install

lint:
	ruff check .
	ruff format --check .

test:
	pytest

deploy:
	./deploy.sh
