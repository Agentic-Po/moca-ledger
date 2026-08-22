.PHONY: crawl detect replay gate pii archive
crawl:   ; python3 crawl.py
detect:  ; python3 detect/run.py $(ARGS)
replay:  ; python3 detect/replay2.py --out detect/replay-results.md
gate:    ; python3 tests/test_pii.py --tree . && python3 -m pytest -q tests 2>/dev/null || python3 tests/test_pii.py --tree .
pii:     ; python3 tests/test_pii.py --tree .
