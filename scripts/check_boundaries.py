"""检查 B站工具箱的基础模块边界。

本脚本只使用 Python 标准库，不执行网络请求、不导入项目模块，也不修改文件。
它检查最容易导致功能杂糅的跨工具导入和 core 反向依赖。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOTS = (ROOT / "app", ROOT / "core", ROOT / "tools")
FEATURES = {"comments", "collector", "monitor", "data_check", "report_center"}


def module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    return ".".join(relative.parts)


def imported_modules(tree: ast.AST) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def feature_for(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "tools" and parts[1] in FEATURES:
        return parts[1]
    return None


def check_file(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return [f"{path.relative_to(ROOT)}: cannot parse file: {exc}"]

    current_module = module_name(path)
    current_parts = current_module.split(".")
    current_feature = (
        current_parts[1]
        if len(current_parts) >= 2
        and current_parts[0] == "tools"
        and current_parts[1] in FEATURES
        else None
    )

    violations: list[str] = []
    for imported in imported_modules(tree):
        imported_feature = feature_for(imported)

        if current_parts[0] == "core" and (
            imported == "app"
            or imported.startswith("app.")
            or imported == "tools"
            or imported.startswith("tools.")
        ):
            violations.append(
                f"{path.relative_to(ROOT)}: core must not depend on {imported}"
            )

        if current_feature and imported_feature and imported_feature != current_feature:
            if current_module != "tools" and current_module != "tools.__init__":
                violations.append(
                    f"{path.relative_to(ROOT)}: {current_feature} must not depend on another tool: {imported}"
                )

    return violations


def main() -> int:
    violations: list[str] = []
    for root in PYTHON_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            violations.extend(check_file(path))

    if violations:
        print("Module boundary check failed:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1

    print("Module boundary check passed: no forbidden core or cross-tool imports found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
