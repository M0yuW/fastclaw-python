# Contributing

Thank you for contributing to FastClaw Python.

## Development workflow

1. Fork or clone the repository and create a focused branch.
2. Use Python 3.12 or newer and install `.[dev]` in a virtual environment.
3. Add tests for behavioral changes.
4. Run `ruff check .`, `ruff format --check .`, `mypy`, and `pytest`.
5. Open a pull request describing the change, its motivation, and validation.

Keep public APIs typed and asynchronous where they perform I/O. New providers
must implement the `fastclaw.providers.Provider` protocol and should not close
the runtime-owned HTTPX client.

By submitting a contribution, you agree that it is licensed under the terms in
[LICENSE](LICENSE).
