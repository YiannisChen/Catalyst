# Contributing

This is a personal graduation project and is not actively seeking contributions. However, issues and questions are welcome.

## Reporting Issues

Please open a GitHub issue with:

- A clear description of the problem
- Steps to reproduce
- Expected vs. actual behavior
- Python/Node version and OS

## Development Setup

```bash
git clone https://github.com/YianniChen/Catalyst.git
cd Catalyst

python -m venv .venv && source .venv/bin/activate
pip install -e packages/data-core -e packages/eval -e packages/agents -e packages/app

cp packages/data-core/.env.template .env
# Edit .env with your API keys
```

## Running Tests

```bash
# All packages
python -m pytest packages/ -q --ignore=packages/data-core/.venv

# Single package
python -m pytest packages/agents/tests/ -q
```

## Commit Style

This project uses [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(agents): add magnitude plausibility guardrail
fix(critic): handle None category values
docs(ADR): add retrieval ordering decision record
chore: update dependencies
```
