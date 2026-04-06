# bootstrap/Makefile — Developer helpers for the bootstrap toolkit

PLATFORM_SCRIPTS := ../platform/scripts
BOOTSTRAP_SCRIPTS := scripts

SHARED := config.py output.py identity.py

.PHONY: sync-shared bootstrap delete help

## Refresh copies of shared scripts from platform/scripts/ (canonical source)
sync-shared:
	@for f in $(SHARED); do \
	  echo "# Canonical source: platform/scripts/$$f — do not modify here directly." > $(BOOTSTRAP_SCRIPTS)/$$f; \
	  echo "# Run 'make sync-shared' in bootstrap/ to refresh from the canonical source." >> $(BOOTSTRAP_SCRIPTS)/$$f; \
	  echo "" >> $(BOOTSTRAP_SCRIPTS)/$$f; \
	  cat $(PLATFORM_SCRIPTS)/$$f >> $(BOOTSTRAP_SCRIPTS)/$$f; \
	  echo "  synced $$f"; \
	done

## Run the bootstrap wizard interactively
bootstrap:
	./bootstrap.sh

## Remove the bootstrapped platform
delete:
	./delete.sh

help:
	@grep -E '^## ' Makefile | sed 's/## //'
