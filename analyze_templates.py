#!/usr/bin/env python
import re
from pathlib import Path

templates_dir = Path('/app/src/frontend/templates')
template_files = list(templates_dir.rglob('*.html'))

print("=" * 70)
print("DJANGO TEMPLATE STATIC ANALYSIS")
print("=" * 70)

issues = []

for template_file in sorted(template_files):
    content = template_file.read_text(encoding='utf-8')
    rel_path = template_file.relative_to(templates_dir)

    # Check for common issues
    line_num = 1
    for line in content.split('\n'):
        # Check for split Django tags (tags that should be on one line)
        if re.search(r'\{%\s*if\s*$', line):
            issues.append(f"{rel_path}:{line_num} - WARNING: 'if' tag split across lines")
        if re.search(r'\{%\s*for\s*$', line):
            issues.append(f"{rel_path}:{line_num} - WARNING: 'for' tag split across lines")

        # Check for improper spacing in comparisons (no spaces around operators)
        if re.search(r'==\S|!=\S|\S==|\S!=', line):
            if '{% if' in line or '{% elif' in line:
                # This might be an issue, check context
                if re.search(r'\{\%\s*(?:if|elif).*[a-zA-Z0-9_]==', line):
                    issues.append(f"{rel_path}:{line_num} - WARNING: Comparison operator may lack spaces: {line.strip()[:80]}")

        # Check for unclosed tags (simple heuristic)
        if '{%' in line and '%}' not in line:
            if not ('else' in line.lower() or 'empty' in line.lower()):
                # Could be split across lines or legitimate
                pass

        line_num += 1

print()
if issues:
    print(f"Found {len(issues)} potential issues:")
    for issue in issues:
        print(f"  • {issue}")
else:
    print("No static analysis issues detected ✓")

print("=" * 70)
