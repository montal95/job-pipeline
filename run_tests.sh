#!/bin/sh
cd /repo
pip install -q langgraph anthropic python-docx pydantic pydantic-settings langchain-anthropic httpx beautifulsoup4 rich typer aiosqlite pytest pytest-asyncio
pip install -q -e .
python -m pytest tests/test_phase3.py -v
