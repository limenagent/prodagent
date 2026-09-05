.PHONY: help play playground test examples lint format

# 项目要求 Python >= 3.11（pyproject），显式用 venv 解释器，不依赖 shell 激活。
PYTHON ?= .venv/bin/python3

help:
	@echo "make play        启动网页 playground（默认 127.0.0.1:8000）"
	@echo "make test        跑全部测试"
	@echo "make examples    依次运行 examples 下的示例"
	@echo "make lint        ruff 检查代码规范（含格式校验，不改文件）"
	@echo "make format      ruff 自动格式化并修复可修复的 lint 问题"

play playground:
	PYTHONPATH=. $(PYTHON) -m src.playground

test:
	PYTHONPATH=. $(PYTHON) -m pytest tests/ -q

examples:
	@for f in examples/0*.py; do echo "--- $$f ---"; PYTHONPATH=. $(PYTHON) $$f; done

# 只管代码目录，README/文档里的代码块不交给 formatter。
lint:
	$(PYTHON) -m ruff check src tests examples
	$(PYTHON) -m ruff format --check src tests examples

format:
	$(PYTHON) -m ruff format src tests examples
	$(PYTHON) -m ruff check --fix src tests examples
