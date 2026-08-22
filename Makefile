.PHONY: crawl detect replay gate pii archive
crawl:   ; python3 crawl.py
detect:  ; python3 detect/run.py $(ARGS)
replay:  ; python3 detect/replay.py $(ARGS)
gate:    ; python3 tests/test_pii.py --tree . && python3 tests/test_gate.py
pii:     ; python3 tests/test_pii.py --tree .
