"""Vietnamese text search that does not depend on the database collation.

SQL ``ILIKE``/``UPPER`` only fold Vietnamese letters when the database's
LC_CTYPE knows them: under the ``C`` locale (and on SQLite) "đạt" does not
match "ĐẠT". The production locale is not known, and changing it is not ours
to do, so matching happens in Python on folded text instead:

- case-insensitive, including Đ/đ;
- accent-insensitive ("dat" finds "Đạt"), like JOYOKR's MySQL
  ``utf8mb4_unicode_ci`` search;
- every word of the query must appear, in any order.

It scans the rows of the already company-scoped and filtered queryset, which
suits OKR's volumes (hundreds to a few thousand rows per company).
"""

from __future__ import annotations

import unicodedata
from typing import Iterable, Optional


def fold(text) -> str:
    """'Đạt MỤC tiêu' -> 'dat muc tieu'."""
    decomposed = unicodedata.normalize("NFD", str(text or ""))
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.replace("đ", "d").replace("Đ", "D").casefold()


def matching_ids(queryset, term: str, fields: Iterable[str]) -> Optional[list]:
    """Primary keys of rows whose ``fields`` contain every word of ``term``;
    ``None`` when the term is blank (no filtering)."""
    words = fold(term).split()
    if not words:
        return None
    fields = list(fields)
    found = []
    for row in queryset.values_list("pk", *fields):
        haystack = " ".join(fold(value) for value in row[1:] if value)
        if all(word in haystack for word in words):
            found.append(row[0])
    return found


def search(queryset, term: str, fields: Iterable[str]):
    """``queryset`` narrowed to rows matching ``term`` (see module doc)."""
    ids = matching_ids(queryset, term, fields)
    return queryset if ids is None else queryset.filter(pk__in=ids)
