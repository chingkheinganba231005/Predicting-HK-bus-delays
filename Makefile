PY ?= python
DB ?= data/collector.sqlite

.PHONY: install collect status dataset train readme all sample test dashboard

install:
	$(PY) -m pip install -r requirements.txt
	$(PY) -m pip install -e . --no-deps

collect:     ## run the collector in the foreground (see docs/collecting.md for unattended runs)
	$(PY) -m hkbus.collector --db $(DB)

status:
	$(PY) scripts/status.py --db $(DB)

dataset:     ## SQLite -> DuckDB, inferred arrivals, features
	$(PY) scripts/01_build_dataset.py --sqlite $(DB)

train:       ## fit, calibrate, evaluate once on the test period
	$(PY) scripts/02_train_evaluate.py

readme:      ## fill README numbers from results/*.json
	$(PY) scripts/render_readme.py

all: dataset train readme

sample:      ## run the whole pipeline on the committed real sample
	$(MAKE) all DB=sample_data/collector_sample.sqlite

test:
	$(PY) -m pytest -q

dashboard:
	streamlit run dashboard/app.py
