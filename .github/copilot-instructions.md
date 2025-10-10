# Code Style Instructions

When writing code, please adhere to the following style guidelines:

* Only use explicit imports (e.g., no `import module`, but `from module import function`).
* Use a functional code style whenever possible
* Keep functions short and built around a single responsibility.
* Use type hints for all function parameters and return types.
* Use double quotes for strings.
* Use docstrings with triple double quotes.
* Use snake_case for methods, including tool_ and prompt_ prefixes.
* Use f-strings only when interpolation is needed.
* Use logging via self.logger instead of print() in production code (tests can print, but preferably on assertions only).
* Use broad exceptions for reliability, but when possible choose explicit exceptions
* Always log exception details.

## Library Specific Guidelines

* Ensure schema introspection code is synced between sync and async versions, but don't refactor it to a common module to keep each version self-contained.
* Stop suggesting shared introspection utility/mixins.
