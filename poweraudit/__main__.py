"""Module entry point so ``python -m poweraudit`` runs the CLI."""

from poweraudit.cli import main

if __name__ == '__main__':
  raise SystemExit(main())
