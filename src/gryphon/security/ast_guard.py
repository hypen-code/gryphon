"""AST-based security guard — static analysis of LLM-generated code before execution."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Any

from gryphon.errors import ASTViolationError, SecurityViolationError
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from gryphon.models import ASTViolationType

logger = get_logger(__name__)

# Modules completely blocked from import
_BLOCKED_MODULES: frozenset[str] = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "shutil",
        "socket",
        "ctypes",
        "pty",
        "tty",
        "termios",
        "signal",
        "resource",
        "multiprocessing",
        "threading",
        "concurrent",
        "pickle",
        "marshal",
        "shelve",
        "importlib",
        "pkgutil",
        "pathlib",
        "glob",
        "tempfile",
        "io",
        "builtins",
        "gc",
        "inspect",
        "dis",
        "code",
        "codeop",
        "pdb",
        "trace",
        "profile",
        "pstats",
        "timeit",
        "ast",
        "tokenize",
        "token",
        "keyword",
        "symtable",
        "urllib",
        "http",
        "xmlrpc",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "requests",
        "aiohttp",
        "tornado",
        "flask",
        "django",
        "fastapi",
        "starlette",
    }
)

# Allowed top-level modules (explicit allowlist)
_ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        "json",
        "datetime",
        "re",
        "math",
        "typing",
        "dataclasses",
        "collections",
        "itertools",
        "functools",
        "operator",
        "string",
        "decimal",
        "fractions",
        "statistics",
        "random",
        "enum",
        "abc",
        "copy",
        "pprint",
        "textwrap",
        "unicodedata",
        "struct",
        "hashlib",
        "hmac",
        "base64",
        "binascii",
        "csv",
        "calendar",
        "__future__",
        # Numeric stacks require an administrator-selected offline profile
        "collections.abc",
    }
)
_NUMERIC_PROFILE_MODULES = frozenset({"numpy", "pandas"})

# Dangerous builtin calls that are not allowed
_BLOCKED_CALLS: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "input",
        "breakpoint",
        "vars",
        "dir",
        "globals",
        "locals",
        "getattr",
        "setattr",
        "delattr",
    }
)

# Dangerous attribute access patterns
_BLOCKED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "__class__",
        "__subclasses__",
        "__globals__",
        "__builtins__",
        "__loader__",
        "__spec__",
        "__dict__",
        "__mro__",
        "__bases__",
        "__import__",
        "environ",  # os.environ access
        "system",
        "popen",
        "spawn",
        "exec_",
        "execve",
        "fork",
        "kill",
        "getenv",
        "setenv",
        "putenv",
        "attrgetter",
        "methodcaller",
    }
)


def available_imports() -> list[str]:
    """Return sorted selectable offline module names, excluding compiler directives.

    Returns:
        A detached list of existing standard and numeric profile names. This is
        a static policy inventory, not a probe of installed host packages.
    """
    return sorted((_ALLOWED_MODULES | _NUMERIC_PROFILE_MODULES) - {"__future__"})


def configured_imports(modules: list[str] | None) -> frozenset[str]:
    """Resolve a narrowing-only offline profile and reject invalid configuration.

    Args:
        modules: None preserves the full existing profile; an empty list denies
            imports. Explicit names must exactly match existing allowed modules.

    Returns:
        Immutable deduplicated module names, retaining __future__ compatibility.

    Raises:
        SecurityViolationError: The setting is malformed or attempts to widen policy.
    """
    maximum = _ALLOWED_MODULES | _NUMERIC_PROFILE_MODULES
    if modules is None:
        return maximum
    if not isinstance(modules, list) or any(not isinstance(module, str) for module in modules):
        raise SecurityViolationError("Invalid offline import profile")
    selected = frozenset(modules)
    if not selected <= maximum:
        raise SecurityViolationError("Invalid offline import profile")
    return selected


class ASTGuard:
    """Static analyzer; runtime isolation remains the authoritative security boundary."""

    def validate(
        self,
        code: str,
        context: str = "",
        additional_allowed_modules: frozenset[str] = frozenset(),
        *,
        allowed_modules: frozenset[str] | None = None,
    ) -> None:
        """Validate Python with an optional narrowing of the server-owned import grant.

        Args:
            code: Python source code to validate.
            context: Compatibility context, never logged as untrusted text.
            additional_allowed_modules: Only numpy/pandas, chosen by the Docker executor.
            allowed_modules: Exact subset of the granted profile; empty denies imports.

        Raises:
            SecurityViolationError: If a blocked pattern or invalid profile is found.
        """
        if (
            not isinstance(additional_allowed_modules, frozenset)
            or not additional_allowed_modules <= _NUMERIC_PROFILE_MODULES
        ):
            raise SecurityViolationError("Invalid offline import profile")
        maximum = _ALLOWED_MODULES | additional_allowed_modules
        if allowed_modules is not None and (
            not isinstance(allowed_modules, frozenset) or not allowed_modules <= maximum
        ):
            raise SecurityViolationError("Invalid offline import profile")
        try:
            tree = ast.parse(code, mode="exec")
        except (SyntaxError, RecursionError, ValueError):
            raise SecurityViolationError("Invalid Python syntax") from None
        visitor = _SecurityVisitor(maximum if allowed_modules is None else allowed_modules)
        visitor.visit(tree)
        if visitor.violations:
            violation = visitor.violations[0]
            logger.warning("security_violation_blocked", violation_type=violation.diagnostic.violation_type)
            raise violation


class _SecurityVisitor(ast.NodeVisitor):
    """AST node visitor that collects security violations."""

    def __init__(self, allowed_modules: frozenset[str]) -> None:
        """Collect only sanitized static violation descriptions."""
        self.violations: list[ASTViolationError] = []
        self._allowed_modules = allowed_modules

    def _add_violation(self, violation_type: ASTViolationType, node: ast.stmt | ast.expr) -> None:
        """Store only the first allowlisted category and its bounded source line."""
        if not self.violations:
            self.violations.append(ASTViolationError(violation_type, node.lineno))

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        """Check top-level import statements."""
        for alias in node.names:
            module = alias.name
            if module.split(".")[0] in _BLOCKED_MODULES:
                self._add_violation("blocked_import", node)
            elif module not in self._allowed_modules:
                # Server function modules must be replaced with broker tool_call capabilities
                # Deny anything else not in allowlist
                self._add_violation("blocked_import", node)  # Fail closed on unknown imports
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        """Check from X import Y statements."""
        module = node.module or ""
        if node.level or module not in self._allowed_modules or module.split(".")[0] in _BLOCKED_MODULES:
            self._add_violation("blocked_import", node)
        if any(alias.name.startswith("_") or alias.name in _BLOCKED_ATTRIBUTES for alias in node.names):
            self._add_violation("blocked_import", node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        """Check function call nodes."""
        # Direct function calls: eval(), exec(), etc.
        if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALLS:
            self._add_violation("blocked_call", node)
        # Method calls: obj.method()
        if isinstance(node.func, ast.Attribute) and node.func.attr in _BLOCKED_ATTRIBUTES:
            self._add_violation("blocked_attribute_call", node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        """Check attribute access nodes."""
        if node.attr in _BLOCKED_ATTRIBUTES or node.attr.startswith("_"):
            self._add_violation("blocked_attribute", node)
        self.generic_visit(node)

    def visit(self, node: ast.AST) -> Any:
        """Prevent builtin aliasing before dispatching ordinary node visitors."""
        if isinstance(node, ast.Name) and (node.id in _BLOCKED_CALLS or node.id.startswith("__")):
            self._add_violation("blocked_call", node)
        return super().visit(node)

    def visit_Global(self, node: ast.Global) -> None:  # noqa: N802
        """Block global statement usage."""
        self._add_violation("blocked_global", node)
        self.generic_visit(node)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:  # noqa: N802
        """Block nonlocal statement usage."""
        self._add_violation("blocked_nonlocal", node)
        self.generic_visit(node)
