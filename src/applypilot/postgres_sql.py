"""Small SQL lexical utilities, independent of the PostgreSQL driver.

The application retains DB-API qmark parameters, not a second SQL dialect.
Quoted text, identifiers, comments and dollar-quoted bodies are never rewritten.
"""
from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=2048)
def bind_query(query: str) -> tuple[str, int]:
    """Convert qmark bind parameters to psycopg placeholders without interpolation."""
    if not isinstance(query, str):
        raise TypeError("SQL must be a string")
    result: list[str] = []
    i = count = 0
    n = len(query)
    while i < n:
        ch = query[i]
        if ch in "'\"":
            quote, start = ch, i
            escape_string = (ch == "'" and i > 0 and query[i-1] in "eE" and
                             (i < 2 or not (query[i-2].isalnum() or query[i-2] == '_')))
            i += 1
            while i < n:
                if query[i] == quote:
                    i += 1
                    if i < n and query[i] == quote:
                        i += 1
                        continue
                    break
                if escape_string and query[i] == "\\":
                    i += 2
                else:
                    i += 1
            result.append(query[start:i].replace('%', '%%'))
        elif query.startswith('--', i):
            end = query.find('\n', i)
            end = n if end < 0 else end + 1
            result.append(query[i:end].replace('%', '%%'))
            i = end
        elif query.startswith('/*', i):
            start, depth = i, 1
            i += 2
            while i < n and depth:
                if query.startswith('/*', i):
                    depth += 1; i += 2
                elif query.startswith('*/', i):
                    depth -= 1; i += 2
                else:
                    i += 1
            result.append(query[start:i].replace('%', '%%'))
        elif ch == '$' and (tag := re.match(r'\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$', query[i:])):
            delimiter = tag.group()
            end = query.find(delimiter, i + len(delimiter))
            if end < 0:
                raise ValueError('Unterminated dollar-quoted SQL body')
            end += len(delimiter)
            result.append(query[i:end].replace('%', '%%'))
            i = end
        elif ch == '?':
            result.append('%s'); count += 1; i += 1
        else:
            result.append('%%' if ch == '%' else ch)
            i += 1
    return ''.join(result), count


def identifier(value: str) -> str:
    """Quote one SQL identifier; callers cannot inject an expression."""
    if not isinstance(value, str) or not value or '\0' in value:
        raise ValueError('Invalid SQL identifier')
    return '"' + value.replace('"', '""') + '"'


def statement_kind(query: str) -> str:
    cleaned = re.sub(r'(?s)/\*.*?\*/|--[^\n]*', '', query).lstrip()
    return cleaned.split(None, 1)[0].upper() if cleaned else ''
