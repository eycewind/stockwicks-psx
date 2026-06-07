import os
import re
import ast
import shutil
from pathlib import Path

APP_ROOT = "/var/www/stockwicks/app"

def get_functions_classes(filepath):
    """Extract function and class names from a Python file using AST."""
    with open(filepath, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=filepath)
    return [node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.ClassDef))]

def find_usages(root_dir, symbols):
    """Find where given functions/classes are used across .py and .html files."""
    matches = {}
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            if filename.endswith((".py", ".html")):
                full_path = os.path.join(dirpath, filename)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    for symbol in symbols:
                        if re.search(rf"\b{symbol}\b", content):
                            matches.setdefault(symbol, []).append(full_path)
                except Exception:
                    continue
    return matches

def update_imports(root_dir, old_module, new_module, dry_run=True):
    """Update import statements from old_module to new_module, and print old vs new lines."""
    import_pattern = re.compile(rf"(from\s+{re.escape(old_module)}\s+import.*|import\s+{re.escape(old_module)}.*)")

    changed_files = []
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            if filename.endswith(".py"):
                full_path = os.path.join(dirpath, filename)
                with open(full_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                new_lines = []
                modified = False
                for line in lines:
                    match = import_pattern.search(line)
                    if match:
                        new_line = line.replace(old_module, new_module)
                        print(f"📌 In {full_path}")
                        print(f"   OLD: {line.strip()}")
                        print(f"   NEW: {new_line.strip()}")
                        if line != new_line:
                            modified = True
                            line = new_line
                    new_lines.append(line)

                if modified:
                    changed_files.append(full_path)
                    if not dry_run:
                        with open(full_path, "w", encoding="utf-8") as f:
                            f.writelines(new_lines)

    return changed_files


def refactor_file(old_path, new_path, dry_run=True):
    """Main driver: move file, detect usages, update imports."""
    # Step 1: Extract functions/classes
    symbols = get_functions_classes(old_path)
    print(f"🔍 Found symbols in {old_path}: {symbols}")

    # Step 2: Find usages
    usages = find_usages(APP_ROOT, symbols)
    for symbol, files in usages.items():
        print(f"⚡ {symbol} is used in:")
        for f in files:
            print(f"   - {f}")

    # Step 3: Build module paths (old vs new)
    old_module = str(Path(old_path).relative_to(APP_ROOT)).replace("/", ".").removesuffix(".py")
    new_module = str(Path(new_path).relative_to(APP_ROOT)).replace("/", ".").removesuffix(".py")

    print(f"\n📦 Old module: {old_module}")
    print(f"📦 New module: {new_module}")

    # Step 4: Update imports
    changed_files = update_imports(APP_ROOT, old_module, new_module, dry_run=dry_run)
    print(f"\n✍️ Imports to update ({'dry run' if dry_run else 'applied'}):")
    for f in changed_files:
        print(f"   - {f}")

    # Step 5: Move file (only if not dry run)
    if not dry_run:
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        shutil.move(old_path, new_path)
        print(f"\n✅ Moved {old_path} → {new_path}")

# Example usage:
if __name__ == "__main__":
    old_file = "/var/www/stockwicks/app/scripts/predict_price_async.py"
    new_file = "/var/www/stockwicks/app/scripts/stocks/predict_price_async.py"
    refactor_file(old_file, new_file, dry_run=True)  # change to False to apply
