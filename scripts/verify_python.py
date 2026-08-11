"""Verify all Python files compile — used by build.bat."""
import py_compile, os, sys

errors = 0
for root, dirs, files in os.walk('backend'):
    dirs[:] = [d for d in dirs if d != '__pycache__']
    for f in files:
        if f.endswith('.py'):
            fp = os.path.join(root, f)
            try:
                py_compile.compile(fp, doraise=True)
            except py_compile.PyCompileError as e:
                print(f'ERROR: {fp}: {e}')
                errors += 1

if errors:
    print(f'{errors} file(s) have syntax errors')
    sys.exit(1)

print(f'All Python files clean')
