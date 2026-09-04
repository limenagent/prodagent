.PHONY: help play playground test examples

# 项目要求 Python >= 3.11（pyproject），显式用 venv 解释器，不依赖 shell 激活。
PYTHON ?= .venv/bin/python3

help:
	@echo "make play        启动网页 playground（默认 127.0.0.1:8000）"
	@echo "make test        跑全部测试"
	@echo "make examples    依次运行 examples 下的示例"

play playground:
	PYTHONPATH=. $(PYTHON) -m src.playground

test:
	PYTHONPATH=. $(PYTHON) -m pytest tests/ -q

examples:
	@for f in examples/0*.py; do echo "--- $$f ---"; PYTHONPATH=. $(PYTHON) $$f; done
