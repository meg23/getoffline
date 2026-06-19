# Enterprise-Safe Python Style

This repository now follows an explicit enterprise-safe Python style:

- No idiomatic shortcuts.
- No comprehensions.
- No lambda expressions.
- No decorators except `@dataclass`.
- No metaprogramming.
- No dynamic attribute access.
- Prefer explicit loops.
- Use full type hints everywhere.
- Keep functions under 30 lines.
- Avoid inheritance.
- Prefer composition.
- One responsibility per class.
- Optimize for readability by junior engineers.

## Rollout policy

When touching existing modules, refactor changed sections to this style.
For broad legacy cleanup, apply incremental refactors per module with tests.
