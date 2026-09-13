# silicon-rl-envs
RL environments for training and evaluating AI agents on real semiconductor engineering tasks.

## Requirements

- Python >= 3.10 (compatible with M1 Mac, CPU-only dev workflow, 8GB RAM friendly)
- Runtime dependencies: none (stdlib only)
- Dev dependencies: `pytest`, `ruff`

## Module layout

```text
silicon_env/              # main package
├── __init__.py           # package version
├── environments/         # environment implementations (M0 placeholder)
│   └── __init__.py
└── agents/               # agent implementations (M0 placeholder)
    └── __init__.py
tests/
└── test_imports.py       # smoke tests: all packages importable, no EDA/Docker/network/keys
```

## Install

```bash
pip install -e .        # runtime only (stdlib, no extra deps)
pip install -e .[dev]   # with pytest + ruff for development
```

## Test

```bash
pytest
```

Default tests require no EDA tools, Docker, network access, or API keys.

## Lint

```bash
ruff check .
```
