#!/usr/bin/env python3
# SCHEDULE: weekly on sunday at 04:00
# ENABLED: true
# DESCRIPTION: Re-index CLAUDE.md, scenes.json, py docs, and .env keys into RAG memory

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import rag_index

rag_index.main()
