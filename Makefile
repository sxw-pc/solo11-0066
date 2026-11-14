PYTHON ?= python

.PHONY: demo run test validate clean

demo:
	$(PYTHON) -m drift_attr demo --config fixtures/rules.yaml --run-id demo-run

run:
	$(PYTHON) -m drift_attr serve --demo

test:
	$(PYTHON) -m pytest -q

validate:
	$(PYTHON) -m drift_attr validate --config fixtures/rules.yaml

clean:
	rm -rf output .pytest_cache
