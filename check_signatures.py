
from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import sys

KNOWN_VARS = {
    "client": ("kalshi_client", "KalshiClient"),
    "conn": None,
}


def resolve(module_name: str, attr: str):
    try:
        return getattr(importlib.import_module(module_name), attr, None)
    except Exception:
        return None


def check_file(path: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError as e:
        return [f"{path.name}: SYNTAX ERROR line {e.lineno}: {e.msg}"]

    imported: dict[str, tuple] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                imported[a.asname or a.name] = (node.module, a.name)

    problems: list[str] = []

    def verify(obj, node, label):
        if obj is None or not callable(obj):
            return
        target = obj.__init__ if inspect.isclass(obj) else obj
        try:
            sig = inspect.signature(target)
        except (ValueError, TypeError):
            return
        params = sig.parameters
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return
        valid = {k for k in params if k != "self"}
        used = {k.arg for k in node.keywords if k.arg}
        unknown = used - valid
        if unknown:
            problems.append(
                f"{path.name}:{node.lineno}  {label}({', '.join(sorted(unknown))}) "
                f"-> not accepted. Valid: {sorted(valid)}")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func

        if isinstance(fn, ast.Name) and fn.id in imported:
            verify(resolve(*imported[fn.id]), node, fn.id)

        elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            spec = KNOWN_VARS.get(fn.value.id)
            if spec:
                cls = resolve(*spec)
                if cls is not None:
                    m = getattr(cls, fn.attr, None)
                    if m is None:
                        problems.append(
                            f"{path.name}:{node.lineno}  {fn.value.id}.{fn.attr} "
                            f"does not exist on {spec[1]}")
                    else:
                        verify(m, node, f"{fn.value.id}.{fn.attr}")
    return problems


def main(argv):
    here = pathlib.Path(__file__).parent
    sys.path.insert(0, str(here))
    files = [pathlib.Path(a) for a in argv[1:]] or sorted(here.glob("*.py"))
    files = [f for f in files if f.name != pathlib.Path(__file__).name]

    all_problems = []
    for f in files:
        all_problems += check_file(f)

    print(f"checked {len(files)} file(s)\n")
    if all_problems:
        for p in all_problems:
            print("  FAIL", p)
        print(f"\n{len(all_problems)} signature mismatch(es)")
        return 1
    print("  no signature mismatches")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
