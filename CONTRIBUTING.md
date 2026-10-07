# Contributing

Thanks for helping improve GitHub Code Crawler.

## Development setup

```powershell
python -m venv .venv
.venv\\Scripts\\activate
pip install -e ".[dev]"
```

## Run tests

```powershell
pytest
```

## Run the crawler without collecting data

```powershell
python main.py --dry-run
```

## Code changes

Keep API access, filtering, processing, security scanning, dataset writing, and CLI behavior separated into their modules. Add or update tests when changing filtering or processing behavior.

Never commit `.env`, tokens, credentials, generated datasets, or raw repository contents.
