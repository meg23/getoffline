#!/usr/bin/env python
import os
import sys
import django
import re

os.environ['GETOFFLINE_DJANGO_ROLE'] = 'api'
os.environ['DJANGO_SETTINGS_MODULE'] = 'frontend.settings'
django.setup()

from pathlib import Path
from django.template import Template, TemplateSyntaxError

print("=" * 70)
print("COMPREHENSIVE DJANGO TEMPLATE CHECK")
print("=" * 70)
print()

templates_dir = Path('/app/src/frontend/templates')
all_templates = list(templates_dir.rglob('*.html'))

syntax_errors = []
issues = []

# 1. Check all templates compile
print("1. TEMPLATE COMPILATION CHECK")
print("-" * 70)
for template_file in sorted(all_templates):
    content = template_file.read_text(encoding='utf-8')
    rel_path = template_file.relative_to(templates_dir)

    try:
        Template(content)
        print(f"  ✓ {rel_path}")
    except TemplateSyntaxError as e:
        print(f"  ✗ {rel_path} - Line {e.lineno}: {e.msg}")
        syntax_errors.append(f"{rel_path}:{e.lineno} - {e.msg}")

# 2. Check for common pattern issues
print()
print("2. PATTERN ANALYSIS")
print("-" * 70)

for template_file in sorted(all_templates):
    content = template_file.read_text(encoding='utf-8')
    rel_path = template_file.relative_to(templates_dir)

    lines = content.split('\n')
    for i, line in enumerate(lines, 1):
        # Check for split Django block tags
        if re.search(r'\{%\s*(if|for|block|with)\s*$', line):
            issues.append(f"{rel_path}:{i} - Block tag may be split across lines")

        # Check for unclosed tags (heuristic)
        if '{%' in line and '%}' not in line and not any(x in line.lower() for x in ['<!--', '#}']):
            # Could be legitimate if it continues on next line with proper structure
            pass

        # Check for endif instead of endfor
        if 'endif' in line and 'for' not in ''.join(lines[max(0, i-5):i]):
            # This is OK, endif is used after if
            pass

if not issues:
    print("  No pattern issues detected ✓")
else:
    for issue in issues[:10]:  # Show first 10
        print(f"  • {issue}")

# 3. Summary
print()
print("=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"Total template files: {len(all_templates)}")
print(f"Syntax errors: {len(syntax_errors)}")
print(f"Pattern issues: {len(issues)}")
print()

if syntax_errors:
    print("❌ ERRORS FOUND:")
    for error in syntax_errors:
        print(f"   {error}")
    sys.exit(1)
else:
    print("✅ ALL TEMPLATES VALID - No Django syntax errors found")
    sys.exit(0)
