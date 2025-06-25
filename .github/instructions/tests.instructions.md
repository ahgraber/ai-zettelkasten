---
applyTo: '**/*.py.**/*.ipynb'
---

# Instructions

When writing unit tests, critically review the provided source code and its associated unit tests.

1. Infer the intended behavior and purpose of the source code. If necessary, ask questions to clarify.
2. Evaluate whether the unit tests accurately validate this intended behavior, rather than simply confirming the current implementation.
3. Identify any gaps in test coverage, such as missing edge cases, incorrect assertions, or lack of negative tests.
4. Propose and explain specific improvements or additions to the unit tests to better align them with the code's intent and ensure robust validation.
5. If applicable, recommend improvements to the source code itself to enhance clarity, testability, or adherence to best practices.

Provide a clear summary of your changes and reasoning.

## Testing Guidelines

- Use `pytest` as the primary testing framework.
- Write comprehensive unit tests for all critical functions, classes, and methods.
- Structure tests using the Arrange-Act-Assert (AAA) pattern.
- Use descriptive names for test functions and test cases.
- Test both positive and negative test cases, including edge cases, boundary conditions, and error scenarios.
- Utilize `pytest` fixtures to set up test environments and share test data.
- Use `pytest.mark.parametrize` to test different inputs and expected outcomes.
- Use markers to categorize tests for selective running.
- Utilize mocks and stubs to isolate the unit under test and avoid external dependencies when testing.
- Test that proper error handling is in place and proper exceptions are thrown.
- Use fixtures to avoid logging inside test functions.
