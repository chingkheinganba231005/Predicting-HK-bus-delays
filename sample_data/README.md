# Sample data

`collector_sample.sqlite` is real data from the collector, 19 November 2026,
18:31 to 20:13 HKT (1.7 hours, 11 routes, 64,877 predictions). Tests and CI
run the whole pipeline on it.

`output/` holds what the pipeline produces from this sample. With 1.7 hours
of data the model outputs are a check that the code runs end to end, not
results: the time split falls inside one evening, so most predictions are
embargoed and the test period is a few minutes long. The project's results
come from the multi-week collection and are written to `results/`.

Source: The Kowloon Motor Bus Co. (1933) Ltd, Citybus Limited and the Hong
Kong Observatory, via DATA.GOV.HK, used under its terms of use.
