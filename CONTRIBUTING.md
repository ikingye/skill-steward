# Contributing

Keep the installable skill package focused. Runtime skill files belong in `skill-steward/`; repository support files such as tests, CI, and this guide belong at the repository root.

Before opening a pull request, run:

```bash
python3 -m unittest discover -s tests
python3 tests/validate_skill.py skill-steward
```

The bundled script must keep using only Python's standard library unless there is a strong reason to add dependencies.

Security-sensitive changes, such as log parsing or path handling, should be reviewed conservatively. Do not add behavior that deletes, moves, or uploads user files without an explicit opt-in flag and tests.
