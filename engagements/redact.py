"""Secret-shaped string detection (spec 6.6).

One detector serves doctor and the write path. It reports *where* a value sits,
never the value and never any text around it: a finding carries a kind from
:data:`KINDS`, the area of the record (``property``, ``section``, ``change_log``
or ``body``), the 1-based index of the section when the line is in one, the
1-based line of the file, and the 1-based occurrence of the finding on that line.
Doctor and the ``possible_secret`` warning are built from that structure alone.

What is detected is written down in the contract and implemented here literally:

- **Assignments.** A keyword from :data:`KEYWORDS` (case-insensitive, with ``_``,
  ``-`` or a space between its words) that starts at a word boundary, optionally
  wrapped in single or double quotes, optional spaces, then ``:`` or ``=``, then a
  value. The value runs to the end of the line or to the start of the next
  keyword and separator on it, trimmed of surrounding quotes and spaces
  (:func:`_trim`). An empty value takes the whole next line, trimmed the same way,
  when that line is indented further than the key's line. Any non-empty value
  that is not a placeholder (:data:`PLACEHOLDERS`) is a finding, whatever it looks
  like: ``password: ask Jane`` is one. Nothing here judges whether a value reads
  as prose.
- **Shapes**, anywhere in a line: a PEM private-key header, an ``AKIA``/``ASIA``
  access key id, an ``sk-``/``sk_live_``/``sk_test_``/``rk_live_``/``pk_live_``
  key, a ``ghp_``/``gho_``/``ghs_``/``github_pat_`` token, an ``xox?-`` Slack
  token, an ``AIza`` key, a three-part JWT starting ``eyJ``, a ``Bearer`` token, a
  URL with a password in its authority, a URL with a credential-named query
  parameter, and a webhook URL on a listed service. Nothing else about a URL is a
  finding.

Matching is on the text as written: assignment keywords match case-insensitively
and nothing else is normalized, so a differently cased copy of a provider prefix,
a query name or a webhook host is not a shape.

Detection is linear by construction. The text is split into lines once. On each
line the assignment starts and every shape are found with patterns that have no
nested quantifier and whose failing attempts cannot reach past a run of their own
characters; a JWT and a URL are located from those runs and read with string
operations, never by running a pattern on a copy of the rest of the line. A value
is cut from match positions. Several empty assignments on one line share a single
read of the next line. Headings and fences are recognized with string operations,
not with the parser's patterns.

The same scan backs the redaction older diagnostics use (:func:`redact_text`,
:func:`redact_label`): a message that quotes record text has each detected span
replaced with :data:`REDACTED`. That is a courtesy for prose diagnostics, not a
promise about any field, and it is the only place a span is ever removed.
"""

import re

from .fsutil import section_key

REDACTED = "[redacted]"

# The fixed kinds. A finding's ``kind`` is always one of these words; none of them
# is derived from the text that matched.
ASSIGNMENT = "password or secret assignment"
PRIVATE_KEY = "private key"
CLOUD_ACCESS_KEY = "cloud access key"
API_KEY = "API key"
ACCESS_TOKEN = "access token"
BEARER_TOKEN = "bearer token"
URL_CREDENTIALS = "URL with embedded credentials"
WEBHOOK_URL = "webhook URL"

KINDS = (ASSIGNMENT, PRIVATE_KEY, CLOUD_ACCESS_KEY, API_KEY, ACCESS_TOKEN, BEARER_TOKEN,
         URL_CREDENTIALS, WEBHOOK_URL)
_KIND_ORDER = {kind: position for position, kind in enumerate(KINDS)}

# The areas a finding can be in. ``section`` and ``change_log`` also carry the
# 1-based index of the section; ``property`` is the frontmatter and ``body`` is
# record text that is in no section.
AREAS = ("property", "section", "change_log", "body")

# Everything the contract calls a placeholder, in one constant: the words,
# compared case-insensitively after trimming; text wrapped in ``<...>``,
# ``[...]`` or ``{...}``; and a run made only of the characters of ``run``.
PLACEHOLDERS = {
    "words": frozenset({"redacted", "none", "n/a", "tbd", "changeme", "example"}),
    "wrappers": frozenset({("<", ">"), ("[", "]"), ("{", "}")}),
    "run": frozenset("*xX\u2022."),
}

# The assignment keywords, longest first so a multi-word keyword wins over its
# last word. ``[ _-]`` is the only separator the contract allows between words.
KEYWORDS = (
    "client[ _-]secret", "access[ _-]token", "access[ _-]key", "auth[ _-]token",
    "private[ _-]key", "secret[ _-]key", "api[ _-]key", "apikey", "passphrase",
    "password", "passwd", "pwd", "secret", "token", "bearer",
)
# An optional quote, a keyword that starts at a word boundary, the same quote, then
# ``:`` or ``=``. The value is not part of the pattern: it is cut from match
# positions, so a line full of assignments costs one pass.
_ASSIGNMENT_START = re.compile(
    r"(?P<quote>[\"']?)(?<!\w)(?:" + "|".join(KEYWORDS) + r")(?P=quote)[ \t]*+[:=]",
    re.IGNORECASE,
)

# The trimming every assigned value gets, wherever it was found.
_TRIM = " \t\"'"

# The shapes that are a literal prefix and a run. A failing attempt reads at most
# the run that follows its own prefix, and a successful one consumes that run, so
# each pattern is one pass over the line.
_SHAPE_PATTERNS = (
    (r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", PRIVATE_KEY),
    (r"(?:AKIA|ASIA)[0-9A-Z]{16,}", CLOUD_ACCESS_KEY),
    (r"(?:sk_live_|sk_test_|rk_live_|pk_live_|sk-)[A-Za-z0-9_-]{16,}", API_KEY),
    (r"(?:github_pat_|ghp_|gho_|ghs_)[A-Za-z0-9_]{20,}", ACCESS_TOKEN),
    (r"xox[a-z]-[A-Za-z0-9-]{10,}", ACCESS_TOKEN),
    (r"AIza[0-9A-Za-z_-]{35,}", API_KEY),
    (r"Bearer[ \t]++[A-Za-z0-9._~+/=-]{16,}", BEARER_TOKEN),
)
_SHAPES = tuple((re.compile(pattern), kind) for pattern, kind in _SHAPE_PATTERNS)
# The redaction of older diagnostics matches the same shapes case-insensitively:
# a diagnostic can quote a section key the parser lower-cased. Findings never use
# these.
_REDACTION_SHAPES = tuple((re.compile(pattern, re.IGNORECASE), kind) for pattern, kind in _SHAPE_PATTERNS)

# A JWT is three dot-separated runs of these characters, the first starting at an
# ``eyJ``. The runs are found once per line.
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_-]++")
_EYJ = re.compile("eyJ")
_EYJ_ANY_CASE = re.compile("eyJ", re.IGNORECASE)

# A URL is ``scheme://`` and a run that ends at whitespace or a delimiter. The
# scheme is read backwards from ``://`` and the rest with string operations.
_URL_BODY = re.compile(r"[^\s<>\"'`]++")
_SCHEME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-")
_SCHEME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_QUERY_CREDENTIALS = frozenset({"token", "key", "api_key", "apikey", "secret", "signature", "sig"})
_QUERY_MINIMUM = 8
_WEBHOOK_PATHS = {"hooks.slack.com": "/services/", "discord.com": "/api/webhooks/",
                  "discordapp.com": "/api/webhooks/"}
_WEBHOOK_HOST_SUFFIX = ".webhook.office.com"

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)


def _trim(value):
    """A value without its surrounding quotes and spaces: the one value rule."""
    return value.strip(_TRIM)


def is_placeholder(value):
    """Whether a value is documentation rather than a credential (:data:`PLACEHOLDERS`)."""
    text = _trim(value)
    if not text:
        return True
    if (text[0], text[-1]) in PLACEHOLDERS["wrappers"]:
        return True
    if set(text) <= PLACEHOLDERS["run"]:
        return True
    return text.casefold() in PLACEHOLDERS["words"]


def location_text(area, section, line, occurrence=None):
    """The structural location a warning or a finding names, in words.

    It is built from the area, the section index, the line and the occurrence
    only, so nothing taken from the record can appear in it.
    """
    if area == "property":
        text = f"line {line} of the property area"
    elif area == "change_log":
        text = f"line {line} of the change log (section {section})"
    elif area == "section":
        text = f"line {line} of section {section}"
    else:
        text = f"line {line} of the body"
    if occurrence is not None:
        text += f", finding {occurrence} on that line"
    return text


# --- layout ---------------------------------------------------------------------


def _indent(line):
    """Count of the spaces and tabs a line starts with."""
    return len(line) - len(line.lstrip(" \t"))


def _fence(line):
    """``(marker, rest)`` when the line opens or closes a fenced block, else None.

    The parser's rule, with string operations: up to three spaces, then a run of
    three or more backticks or tildes.
    """
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3 or stripped[:1] not in ("`", "~"):
        return None
    run = len(stripped) - len(stripped.lstrip(stripped[0]))
    if run < 3:
        return None
    return stripped[:run], stripped[run:]


def _heading(line):
    """``(level, title)`` for a level-1 or level-2 heading, else None.

    The parser's rule, with string operations: up to three spaces, one or two
    ``#``, then whitespace or the end of the line. A closing run of ``#`` is
    dropped from the title when it is the whole title or follows whitespace.
    """
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return None
    level = len(stripped) - len(stripped.lstrip("#"))
    rest = stripped[level:]
    if level not in (1, 2) or (rest and rest[0] not in " \t"):
        return None
    title = rest
    end = title.rstrip(" \t")
    body = end.rstrip("#")
    if len(body) < len(end) and (not body or body[-1] in " \t"):
        title = body
    return level, title.strip()


def _layout(lines):
    """``(areas, section numbers)`` for every line of a record, without the text.

    ``property`` is the frontmatter, ``change_log`` is the section whose heading
    reads "Change log", ``section`` is any other ``##`` section and ``body`` is
    everything else. Sections follow the parser: they exist only after a
    frontmatter block, a ``##`` heading outside a fenced block starts one, a ``#``
    heading ends it, and they are numbered from 1 in file order.
    """
    count = len(lines)
    areas = ["body"] * count
    numbers: list = [None] * count
    if not count or lines[0].removeprefix("\ufeff") != "---":
        return areas, numbers
    closing = next((index for index in range(1, count) if lines[index] == "---"), None)
    if closing is None:
        return areas, numbers
    for index in range(closing + 1):
        areas[index] = "property"
    current = None
    fence = None
    number = 0
    for index in range(closing + 1, count):
        line = lines[index]
        marker = _fence(line)
        if marker is not None:
            symbol, rest = marker
            if fence is None and (symbol[0] != "`" or "`" not in rest):
                fence = symbol
                marker = None
            elif (fence is not None and symbol[0] == fence[0] and len(symbol) >= len(fence)
                    and not rest.strip(" \t")):
                fence = None
                marker = None
        if marker is None and fence is None:
            heading = _heading(line)
            if heading is not None:
                if heading[0] == 1:
                    current = None
                else:
                    number += 1
                    current = (number, section_key(heading[1]) == "change log")
        if current is not None:
            numbers[index] = current[0]
            areas[index] = "change_log" if current[1] else "section"
    return areas, numbers


# --- matching on one line -------------------------------------------------------


def _jwt_spans(line, eyj):
    """``(start, stop)`` of each JWT on the line, in order, without overlap.

    The line's token runs are found once. A JWT starts at the first ``eyJ`` in a
    run when the next two runs each follow a single dot; it ends where the third
    run ends, and the next search starts after it.
    """
    runs = [(match.start(), match.end()) for match in _TOKEN_RUN.finditer(line)]
    spans = []
    index = 0
    while index + 2 < len(runs):
        start, end = runs[index]
        second, third = runs[index + 1], runs[index + 2]
        if (second[0] == end + 1 and line[end] == "." and third[0] == second[1] + 1
                and line[second[1]] == "."):
            found = eyj.search(line, start, end)
            if found is not None:
                spans.append((found.start(), third[1]))
                index += 3
                continue
        index += 1
    return spans


def _url_kind(url):
    """The kind of credential a URL carries, or None. String work, no pattern.

    The fragment is dropped first, so a ``?`` after ``#`` is not a query. A
    webhook host and path win over a password or a query parameter; names and
    hosts are compared as written.
    """
    rest = url.partition("://")[2].partition("#")[0]
    end = len(rest)
    for char in "/?":
        found = rest.find(char)
        if 0 <= found < end:
            end = found
    authority, tail = rest[:end], rest[end:]
    userinfo, _at, host = authority.rpartition("@")
    host = host.partition(":")[0]
    path, _question, query = tail.partition("?")
    prefix = _WEBHOOK_PATHS.get(host)
    if (prefix is not None and path.startswith(prefix)) or host.endswith(_WEBHOOK_HOST_SUFFIX):
        return WEBHOOK_URL
    _user, colon, password = userinfo.partition(":")
    if colon and not is_placeholder(password):
        return URL_CREDENTIALS
    for pair in query.split("&"):
        name, equals, value = pair.partition("=")
        if equals and name in _QUERY_CREDENTIALS and len(value) >= _QUERY_MINIMUM and not is_placeholder(value):
            return URL_CREDENTIALS
    return None


def _url_spans(line):
    """``(start, stop, kind)`` for each URL on the line that carries a credential.

    ``://`` is found with a string search; the scheme is the run of scheme
    characters before it, from its first letter, and never reaches back past the
    previous URL; the URL runs to the first whitespace or delimiter.
    """
    spans = []
    floor = 0
    position = line.find("://")
    while position >= 0:
        start = position
        while start > floor and line[start - 1] in _SCHEME_CHARS:
            start -= 1
        while start < position and line[start] not in _SCHEME_START:
            start += 1
        body = _URL_BODY.match(line, position + 3) if start < position else None
        if body is None:
            position = line.find("://", position + 1)
            continue
        kind = _url_kind(line[start:body.end()])
        if kind is not None:
            spans.append((start, body.end(), kind))
        floor = body.end()
        position = line.find("://", floor)
    return spans


def _next_value(lines, index):
    """``(start, stop, value)`` for the line after ``lines[index]``, or None.

    That line is a value for an empty assignment only when it is indented further
    than the key's line; an empty value is a placeholder to the caller. Called at
    most once per line.
    """
    if index + 1 >= len(lines):
        return None
    following = lines[index + 1]
    if _indent(following) <= _indent(lines[index]):
        return None
    value = _trim(following)
    start = len(following) - len(following.lstrip(" \t"))
    return start, len(following.rstrip(" \t")), value


# --- the scan -------------------------------------------------------------------


def _lines(text):
    """The text's lines without their EOLs, and the offset each starts at."""
    lines = text.split("\n")
    offsets = []
    position = 0
    for number, line in enumerate(lines):
        offsets.append(position)
        position += len(line) + 1
        if line.endswith("\r"):
            lines[number] = line[:-1]
    return lines, offsets


def _scan(text, *, any_case=False):
    """``(lines, findings)``: the text's lines and every finding in it, in file order.

    Each finding is ``(line index, start, stop, kind, value)``: the 0-based line,
    the span in ``text``, the kind and the matched value (the trimmed value of an
    assignment, the matched text of a shape or a URL). This is the only walk of the
    patterns and it is one pass: a line's assignments and shapes are found once,
    and an empty assignment's value is read from the next line once per line, so
    several of them share the same value object.
    """
    lines, offsets = _lines(text)
    shapes = _REDACTION_SHAPES if any_case else _SHAPES
    eyj = _EYJ_ANY_CASE if any_case else _EYJ
    found = []
    carried = []
    for index, line in enumerate(lines):
        offset = offsets[index]
        on_line = []
        starts = list(_ASSIGNMENT_START.finditer(line))
        shared = None
        next_entries = []
        for position, match in enumerate(starts):
            stop = starts[position + 1].start() if position + 1 < len(starts) else len(line)
            raw = line[match.end():stop]
            value = _trim(raw)
            if value:
                if not is_placeholder(value):
                    lead = len(raw) - len(raw.lstrip(" \t"))
                    on_line.append((index, offset + match.end() + lead,
                                    offset + match.end() + len(raw.rstrip(" \t")), ASSIGNMENT, value))
                continue
            if shared is None:
                following = _next_value(lines, index)
                shared = False if following is None or is_placeholder(following[2]) else following
            if shared:
                next_offset = offsets[index + 1]
                next_entries.append((index + 1, next_offset + shared[0], next_offset + shared[1],
                                     ASSIGNMENT, shared[2]))
        for pattern, kind in shapes:
            for match in pattern.finditer(line):
                on_line.append((index, offset + match.start(), offset + match.end(), kind, match.group()))
        for start, stop in _jwt_spans(line, eyj):
            on_line.append((index, offset + start, offset + stop, ACCESS_TOKEN, line[start:stop]))
        for start, stop, kind in _url_spans(line):
            on_line.append((index, offset + start, offset + stop, kind, line[start:stop]))
        on_line.sort(key=lambda item: (item[1], _KIND_ORDER[item[3]]))
        found.extend(carried)
        found.extend(on_line)
        carried = next_entries
    return lines, found


def _structural(lines, occurrences):
    """The public shape of each finding: no span, no value, nothing from the text.

    One walk over the findings, which arrive in line order, counts the occurrence
    on each line.
    """
    areas, numbers = _layout(lines)
    out = []
    last_line, count = None, 0
    for index, _start, _stop, kind, _value in occurrences:
        count = count + 1 if index == last_line else 1
        last_line = index
        out.append({"line": index + 1, "area": areas[index], "section": numbers[index],
                    "kind": kind, "occurrence": count})
    return out


def secret_findings(text):
    """One entry per finding in ``text``, with no text taken from ``text``.

    Each entry is ``{"line", "area", "section", "kind", "occurrence"}``: the 1-based
    line of the file, the area, the 1-based section index when the line is in one
    (else None), a kind from :data:`KINDS`, and the 1-based position of the finding
    among the findings on that line, so two findings of one kind on one line stay
    two.
    """
    return _structural(*_scan(text))


def new_findings(previous, current):
    """Findings in ``current`` whose (kind, matched value) is not in ``previous``.

    The comparison is between two sets of findings, never positions: a value that
    was already in the record does not warn again wherever it moved, a value that
    was added and removed in the same call is not in ``current`` at all, and a
    value assembled across two ops is new. Values are compared after the one
    trimming rule, exactly as written otherwise. Entries have the shape
    :func:`secret_findings` returns, with occurrences counted over all findings on
    the line.
    """
    known = {(kind, value) for _index, _start, _stop, kind, value in _scan(previous)[1]}
    lines, found = _scan(current)
    return [entry for entry, item in zip(_structural(lines, found), found, strict=True)
            if (item[3], item[4]) not in known]


# --- redaction of older diagnostics ---------------------------------------------


def looks_secret(value):
    """True when a whole label (a key, a heading, an id) holds a secret shape."""
    return isinstance(value, str) and bool(_scan(value, any_case=True)[1])


def redact_label(value):
    """A diagnostic label: kept as is, or replaced wholesale when it looks like a secret."""
    if looks_secret(value):
        return REDACTED
    return value


def _merged(spans):
    """Union of spans, so overlapping matches are replaced once and completely."""
    merged = []
    for start, stop in sorted(spans):
        if merged and start <= merged[-1][1]:
            if stop > merged[-1][1]:
                merged[-1] = (merged[-1][0], stop)
        else:
            merged.append((start, stop))
    return merged


def _block_spans(text):
    """The whole of each private-key block, for masking in a diagnostic.

    A block runs from a BEGIN marker to the first END marker after it, or to the
    end of the text. The END markers are found once and paired in one forward
    walk, so unmatched BEGIN markers do not each search the rest of the text.
    """
    ends = [(match.start(), match.end()) for match in _PEM_END.finditer(text)]
    spans = []
    pointer = 0
    for match in _PEM_BEGIN.finditer(text):
        while pointer < len(ends) and ends[pointer][0] < match.end():
            pointer += 1
        spans.append((match.start(), ends[pointer][1] if pointer < len(ends) else len(text)))
    return spans


def redact_text(text):
    """Replace every secret-shaped span in a diagnostic message, keeping the rest.

    Relative paths, line numbers and reasons stay readable; only the value is
    replaced, so a finding still says where to look.
    """
    if not isinstance(text, str):
        return text
    spans = _merged([(item[1], item[2]) for item in _scan(text, any_case=True)[1]]
                    + _block_spans(text))
    parts, last = [], 0
    for start, stop in spans:
        parts.append(text[last:start])
        parts.append(REDACTED)
        last = stop
    parts.append(text[last:])
    return "".join(parts)
