.PHONY: install-hooks lint test

install-hooks:
	lefthook install

lint:
	ruff check .
	ruff format --check .

test:
	pytest
