"""Lossless record parsing and in-memory, byte-splicing operation application."""

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken, TagToken

from .errors import EngagementsError
from .fsutil import normalize_text, section_key


def _invalid(index, field, reason):
    """An error attributable to one op in the request."""
    raise EngagementsError("invalid_op", "Invalid operation.",
                           {"op_index": index, "field": field, "reason": reason})


def _invalid_result(field, reason):
    """An error about the request or its result, not about one op."""
    raise EngagementsError("invalid_op", "Invalid operation.",
                           {"field": field, "reason": reason})


def _single(value, maximum):
    return (isinstance(value, str) and 0 < len(value) <= maximum and bool(value.strip())
            and all(not (ord(c) < 32 or 127 <= ord(c) <= 159 or
                         0xD800 <= ord(c) <= 0xDFFF or ord(c) in (0x2028, 0x2029, 0xFFFE, 0xFFFF))
                    for c in value))


def _check_text(value, index, field):
    fence = None
    for line in value.replace("\r\n", "\n").split("\n"):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            symbol, rest = marker.groups()
            if fence is None and (symbol[0] != "`" or "`" not in rest):
                fence = symbol
                continue
            if fence and symbol[0] == fence[0] and len(symbol) >= len(fence) and not rest.strip(" \t"):
                fence = None
                continue
        if fence is None and re.match(r"^ {0,3}#{1,2}(?:[ \t]|$)", line):
            _invalid(index, field, "heading_in_text")
    if fence:
        _invalid(index, field, "unbalanced_fence")


def _validate_shapes(ops):
    """Stage 1: per-op schemas, single-line and length rules, reserved names."""
    if not isinstance(ops, list) or not 1 <= len(ops) <= 50:
        _invalid_result("ops", "invalid_value")
    shapes = {"replace_section": ("section", "body"), "append_to_section": ("section", "text"),
              "replace_line": ("section", "match", "text"), "remove_line": ("section", "match")}
    # Validate every request shape before consulting the resolved record.
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            _invalid(index, "op", "invalid_value")
        if op.get("key") == "updated":
            _invalid(index, "key", "plugin_owned")
        if isinstance(op.get("section"), str) and section_key(op["section"]) == "change log":
            _invalid(index, "section", "plugin_owned")
        if "op" not in op:
            _invalid(index, "op", "missing_field")
        name = op["op"]
        if not isinstance(name, str) or name not in (*shapes, "set_property"):
            _invalid(index, "op", "invalid_value")
        if name == "set_property":
            if "key" not in op:
                _invalid(index, "key", "missing_field")
            key = op["key"]
            if key not in ("name", "status", "client", "aliases", "locations"):
                _invalid(index, "key", "invalid_value")
            fields = ("key", "values" if key in ("aliases", "locations") else "value")
        else:
            fields = shapes[name]
        for field_name in fields:
            if field_name not in op:
                _invalid(index, field_name, "missing_field")
            value = op[field_name]
            if field_name == "key":
                continue
            if field_name == "values":
                valid = isinstance(value, list) and len(value) <= 50 and all(_single(v, 1024) for v in value)
            elif field_name in ("section", "value"):
                valid = _single(value, 200)
            elif field_name == "match":
                valid = isinstance(value, str) and 1 <= len(value) <= 1000
            elif name == "replace_line":
                valid = _single(value, 1000)
            else:
                valid = (isinstance(value, str) and len(value) <= 20000
                         and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))
            if not valid:
                _invalid(index, field_name, "invalid_value")
        for field_name in op:
            if field_name not in ("op", *fields):
                _invalid(index, field_name, "unexpected_field")


def _validate_values(kind, ops, *, client_exists):
    """Stage 4: kind-specific values and the rules for inserted text."""
    shapes = {"replace_section": ("section", "body"), "append_to_section": ("section", "text"),
              "replace_line": ("section", "match", "text"), "remove_line": ("section", "match")}
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            _invalid(index, "op", "invalid_value")
        if op.get("key") == "updated":
            _invalid(index, "key", "plugin_owned")
        if isinstance(op.get("section"), str) and section_key(op["section"]) == "change log":
            _invalid(index, "section", "plugin_owned")
        name = op.get("op")
        if name == "set_property":
            key = op.get("key")
            if key not in ("name", "status", "client", "aliases", "locations"):
                _invalid(index, "key", "missing_field" if key is None else "invalid_value")
            fields = ("key", "values" if key in ("aliases", "locations") else "value")
        elif name in shapes:
            fields = shapes[name]
        else:
            _invalid(index, "op", "missing_field" if name is None else "invalid_value")
        for field_name in fields:
            if field_name not in op:
                _invalid(index, field_name, "missing_field")
        for field_name in op:
            if field_name not in ("op", *fields):
                _invalid(index, field_name, "unexpected_field")
        if name == "set_property":
            key = op["key"]
            if key in ("client", "locations") and kind != "project":
                _invalid(index, "key", "key_not_allowed_for_kind")
            if "values" in fields:
                values = op["values"]
                if not isinstance(values, list) or len(values) > 50 or any(not _single(v, 1024) for v in values):
                    _invalid(index, "values", "invalid_value")
            else:
                value = op["value"]
                if not _single(value, 200):
                    _invalid(index, "value", "invalid_value")
                if key == "status" and value not in ({"active", "paused", "former"} if kind == "client" else {"active", "historical"}):
                    _invalid(index, "value", "status_not_allowed_for_kind")
                if key == "client" and (not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", value) or not client_exists(value)):
                    raise EngagementsError("not_found", "Client not found.", {"reason": "no_match"})
            continue
        if not _single(op["section"], 200):
            _invalid(index, "section", "invalid_value")
        if "match" in fields and (not isinstance(op["match"], str) or not 1 <= len(op["match"]) <= 1000):
            _invalid(index, "match", "invalid_value")
        for text_field in ("body", "text"):
            if text_field in fields:
                text = op[text_field]
                maximum = 1000 if name == "replace_line" else 20000
                if (not isinstance(text, str) or len(text) > maximum
                        or any(0xD800 <= ord(c) <= 0xDFFF for c in text)
                        or (name == "replace_line" and not _single(text, maximum))):
                    _invalid(index, text_field, "invalid_value")
                _check_text(text, index, text_field)
                if name == "append_to_section" and not text.strip():
                    _invalid(index, text_field, "empty_text")


def validate_ops(kind, ops, *, client_exists):
    """Validate request operation shapes and values without mutating input.

    ``kind`` is the resolved record kind. Callers resolve it before stage 4;
    anything else is a caller bug, like an unknown ``library_defects`` value,
    so it raises ``ValueError`` rather than a user-facing error.  Shape errors
    run before kind-specific values, matching TOOLS.md error precedence.
    """
    if kind not in ("client", "project"):
        raise ValueError("kind must be 'client' or 'project'")
    _validate_shapes(ops)
    _validate_values(kind, ops, client_exists=client_exists)


def validate_shapes(ops):
    """Stage 1 of ``validate_ops`` alone, for callers that must run it first."""
    _validate_shapes(ops)


def validate_values(kind, ops, *, client_exists):
    """The kind-specific half of ``validate_ops`` (TOOLS.md stage 4).

    Callers run it after ``validate_shapes`` and after their own global-defect
    check, so that ``malformed`` takes precedence over a value error exactly as
    ``apply_ops`` orders them.
    """
    if kind not in ("client", "project"):
        raise ValueError("kind must be 'client' or 'project'")
    _validate_values(kind, ops, client_exists=client_exists)


def is_single_line(value, maximum):
    """The shared single-line string rule of TOOLS.md "Shared value formats"."""
    return _single(value, maximum)


def derive_id(name, kind, taken):
    normalized = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", name))
    normalized = "".join(c for c in normalized if not unicodedata.combining(c)).casefold()
    base = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:63].rstrip("-")
    if len(base) < 2:
        base = f"{kind}-{base}" if base else kind
    candidate = base
    number = 2
    while candidate in taken:
        suffix = f"-{number}"
        candidate = base[:63 - len(suffix)].rstrip("-") + suffix
        number += 1
    return candidate


def derive_filename(name, id, taken):
    component = unicodedata.normalize("NFC", name)
    component = "".join(
        " " if unicodedata.category(c) == "Cc" or c in '<>:"/\\|?*' else c
        for c in component
    )
    component = " ".join(component.split()).strip(" .")[:80]
    component = component.encode("utf-8")[:200].decode("utf-8", errors="ignore").rstrip(" .") or id
    reserved = {
        "con", "prn", "aux", "nul",
        *(f"{prefix}{n}" for prefix in ("com", "lpt") for n in "0123456789¹²³"),
    }
    prefix = component.split(".", 1)[0].rstrip(" ")
    if prefix.casefold() in reserved:
        component = component[:len(prefix)] + "_" + component[len(prefix):]
    normalized_taken = {unicodedata.normalize("NFC", item).casefold() for item in taken}
    candidate = component
    number = 2
    while unicodedata.normalize("NFC", candidate).casefold() in normalized_taken:
        candidate = f"{component} ({number})"
        number += 1
    return candidate


@dataclass
class Section:
    title: str
    key: str
    start: int
    body_start: int
    end: int


@dataclass
class Record:
    data: bytes
    is_record: bool = False
    kind: str | None = None
    id: str | None = None
    name: str | None = None
    frontmatter_span: tuple | None = None
    global_defects: set = field(default_factory=set)
    property_defects: dict = field(default_factory=dict)
    section_defects: dict = field(default_factory=dict)
    bom: bool = False
    eol: bytes = b"\n"
    final_newline: bool = False
    properties: dict | None = field(default_factory=dict)
    property_spans: dict = field(default_factory=dict)
    duplicate_keys: set = field(default_factory=set)
    unsupported_keys: set = field(default_factory=set)
    mistyped_keys: set = field(default_factory=set)
    name_unreadable: bool = False
    sections: list = field(default_factory=list)
    defects: set = field(default_factory=set)


def _yaml_unsupported(text):
    try:
        if any(isinstance(t, (AliasToken, AnchorToken, TagToken)) for t in yaml.scan(text)):
            return True
        node = yaml.compose(text)
        if not isinstance(node, MappingNode) or len(node.value) != 1:
            return True
        value = node.value[0][1]
        if isinstance(value, MappingNode):
            return True
        if isinstance(value, ScalarNode):
            return value.style in ("|", ">")
        if not isinstance(value, SequenceNode):
            return True
        return any(
            not isinstance(item, ScalarNode)
            or any(c in text[item.start_mark.index:item.end_mark.index] for c in "\r\n\x85\u2028\u2029")
            for item in value.value
        )
    except yaml.YAMLError:
        return True


def _file_lines(data):
    """Split only on file EOLs, not YAML's other line-break characters."""
    parts = data.split(b"\n")
    return [part + b"\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _line_content(line):
    if line.endswith(b"\n"):
        return line[:-1].removesuffix(b"\r")
    return line


def _segment_length(segment):
    if segment[0] == "base":
        return segment[2] - segment[1]
    return len(segment[1])


def _split_segments(segments, offset):
    """Split a segment list at an offset in the current document."""
    left, right, position = [], [], 0
    for segment in segments:
        length = _segment_length(segment)
        if position + length <= offset:
            left.append(segment)
        elif position >= offset:
            right.append(segment)
        else:
            cut = offset - position
            if segment[0] == "base":
                _, start, end = segment
                left.append(("base", start, start + cut))
                right.append(("base", start + cut, end))
            else:
                _, payload = segment
                left.append(("new", payload[:cut]))
                right.append(("new", payload[cut:]))
        position += length
    return left, right


class _ByteGuard:
    """Authorise every splice and prove untouched bytes are unchanged.

    ``step`` wraps each mutation: it receives the bytes before and after the
    edit plus the interval the contract allows that operation to change, with a
    zero-width interval for an insertion. The interval is derived from the
    pre-edit record by the caller, never from the helper that produced the new
    bytes, so a splice that reaches a neighbouring property, frontmatter
    comment, level-1 content or untargeted section body is rejected. The
    segment map built from the validated splices then proves, at the end, that
    every byte outside the edited intervals reappears identically.
    """

    def __init__(self, base):
        self.base = base
        self.segments = [("base", 0, len(base))]

    def step(self, before, after, start, end):
        prefix = before[:start]
        suffix = before[end:]
        if (len(after) < len(prefix) + len(suffix)
                or not after.startswith(prefix) or not after.endswith(suffix)):
            _structure_changed()
        self._splice(start, end, after[len(prefix):len(after) - len(suffix)])

    def _splice(self, start, end, replacement):
        left, rest = _split_segments(self.segments, start)
        _, right = _split_segments(rest, end - start)
        self.segments = left + [("new", replacement)] + right

    def verify(self, final):
        position = 0
        rebuilt = []
        covered = 0
        for segment in self.segments:
            if segment[0] == "new":
                rebuilt.append(segment[1])
                position += len(segment[1])
                continue
            _, start, end = segment
            if start < covered or end > len(self.base):
                _structure_changed()
            covered = end
            chunk = self.base[start:end]
            if final[position:position + len(chunk)] != chunk:
                _structure_changed()
            rebuilt.append(chunk)
            position += len(chunk)
        if position != len(final) or b"".join(rebuilt) != final:
            _structure_changed()


def parse(data: bytes) -> Record:
    return _parse(data, 65536)


def _parse(data: bytes, frontmatter_limit: int) -> Record:
    result = Record(data=data, bom=data.startswith(b"\xef\xbb\xbf"), final_newline=data.endswith(b"\n"))
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    if crlf and lf or b"\r" in data.replace(b"\r\n", b""):
        result.defects.add("mixed_eol")
    result.eol = b"\r\n" if crlf else b"\n"
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        result.defects.add("encoding")
        result.global_defects = result.defects
        result.properties = None
        # Recognition is limited to the original bytes of the frontmatter;
        # invalid bytes in the body must not make an otherwise marked file vanish.
        prefix = data[:65536]
        raw = _file_lines(prefix)
        first = _line_content(raw[0]).removeprefix(b"\xef\xbb\xbf") if raw else b""
        if first == b"---":
            size = len(raw[0])
            for line in raw[1:]:
                size += len(line)
                if _line_content(line) == b"---" and (line.endswith(b"\n") or size == len(data)):
                    safe = data[:size].decode("utf-8", errors="replace").encode("utf-8")
                    # This prefix was already bounded in original bytes. UTF-8
                    # replacement characters may expand it beyond that bound.
                    inspected = _parse(safe, len(safe))
                    result.is_record = inspected.is_record
                    result.kind = inspected.kind
                    result.frontmatter_span = (0, size)
                    break
        return result
    raw_lines = _file_lines(data)
    offsets = []
    position = 0
    for line in raw_lines:
        offsets.append(position)
        position += len(line)
    lines = [_line_content(line).decode("utf-8") for line in raw_lines]
    if result.bom and lines:
        lines[0] = lines[0].removeprefix("\ufeff")
    if not lines or lines[0] != "---":
        return result
    closing = next((i for i in range(1, len(lines)) if lines[i] == "---" and
                    offsets[i] + len(raw_lines[i]) <= frontmatter_limit), None)
    if closing is None:
        return result
    start_block = offsets[1]
    end_block = offsets[closing]
    result.frontmatter_span = (0, offsets[closing] + len(raw_lines[closing]))
    block = data[start_block:end_block].decode("utf-8")
    try:
        node = yaml.compose(block)
        events = list(yaml.parse(block))
        if not isinstance(node, MappingNode):
            return result
        # Walk the event tree: aliases have their own end marks, unlike nodes.
        index = 3
        def consume(pos):
            event = events[pos]
            if isinstance(event, (yaml.events.SequenceStartEvent, yaml.events.MappingStartEvent)):
                pos += 1
                last_item_end = None
                while not isinstance(events[pos], (yaml.events.SequenceEndEvent, yaml.events.MappingEndEvent)):
                    pos, last_item_end = consume(pos)
                # spec C6: a block sequence ends at its last item. PyYAML puts the
                # block sequence-end mark after any trailing comment and blank
                # lines, and those bytes belong to no property.
                if (isinstance(event, yaml.events.SequenceStartEvent) and not event.flow_style
                        and last_item_end is not None):
                    return pos + 1, last_item_end
                return pos + 1, events[pos].end_mark.index
            return pos + 1, event.end_mark.index
        pairs = []
        for key_node, value_node in node.value:
            key_event = events[index]
            index, _ = consume(index)
            value_event = events[index]
            index, value_end = consume(index)
            key = key_node.value if isinstance(key_node, ScalarNode) else str(key_node.value)
            key_index = key_event.start_mark.index
            line_start = block.rfind("\n", 0, key_index) + 1
            line_end = block.find("\n", value_end)
            if value_end and block[value_end - 1:value_end] == "\n":
                line_end = value_end - 1
            elif line_end < 0:
                line_end = len(block) - 1
            end_char = min(len(block), line_end + 1)
            span = (start_block + len(block[:line_start].encode()), start_block + len(block[:end_char].encode()))
            result.property_spans.setdefault(key, []).append(span)
            source = block[line_start:end_char]
            canonical = (isinstance(key_node, ScalarNode) and key_node.style is None and
                         key_index == line_start and block[key_node.end_mark.index:key_node.end_mark.index+1] == ":")
            unsupported = not canonical or _yaml_unsupported(source)
            unsupported |= any(c in source for c in "\x85\u2028\u2029")
            unsupported |= any(not line.strip() or line.lstrip().startswith("#") for line in source.split("\n")[:-1])
            if isinstance(value_node, ScalarNode):
                value_source = block[value_event.start_mark.index:value_end]
                unsupported |= any(c in value_source for c in "\r\n\x85\u2028\u2029")
                unsupported |= "\n" in block[key_index:value_event.start_mark.index]
            if unsupported:
                result.unsupported_keys.add(key)
            pairs.append((key, value_node, span))
        # Two properties' spans share a file line when their byte ranges overlap.
        # Spans come in document order and are never empty (each covers at least
        # its own line), so sorting by start makes one sweep exact: a span overlaps
        # another when it starts inside a span that began earlier, or when the next
        # span starts inside it. The pairwise scan this replaces was quadratic in the
        # number of frontmatter keys.
        ordered = sorted(pairs, key=lambda pair: pair[2][0])
        earlier_end = -1
        for position, (key, _, span) in enumerate(ordered):
            overlapping = earlier_end > span[0]
            if position + 1 < len(ordered) and span[1] > ordered[position + 1][2][0]:
                overlapping = True
            if overlapping:
                result.unsupported_keys.add(key)
            earlier_end = max(earlier_end, span[1])
        result.duplicate_keys = {key for key, spans in result.property_spans.items() if len(spans) > 1}
        # Construct each value independently, keeping duplicate keys visible.
        loader = yaml.SafeLoader(block)
        unsupported_value = object()
        try:
            values = []
            for key, value, _ in pairs:
                try:
                    converted = loader.construct_object(value, deep=True)
                except RecursionError:
                    # Contract (e14081a): recursion is a global yaml_parse defect.
                    raise
                except Exception:
                    # SafeLoader constructors fail in many ways on explicit tags
                    # (``!!timestamp nope`` raises AttributeError, ``!!int ""``
                    # IndexError). Whatever the type, the failure belongs to this
                    # one property: a targeted unsupported_yaml defect, escalated
                    # only when the property is an identity field.
                    converted = (unsupported_value, type(value).__name__)
                    # A failed constructor leaves its in-progress nodes marked
                    # recursive; clear them so later properties construct cleanly.
                    loader.recursive_objects.clear()
                values.append((key, converted))
        finally:
            loader.dispose()
        markers = [node.value if isinstance(node, ScalarNode) and node.value in ("client", "project") else value
                   for (key, node, _), (_, value) in zip(pairs, values, strict=True) if key == "engagements"]
        result.is_record = any(v in ("client", "project") for v in markers)
        if not result.is_record:
            return result
        result.kind = markers[0] if all(v == markers[0] for v in markers) else None
        def json_value(value):
            if isinstance(value, tuple) and len(value) == 2 and value[0] is unsupported_value:
                return {"unsupported_yaml": value[1]}
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            if isinstance(value, float) and not math.isfinite(value):
                return {"unsupported_yaml": "float"}
            if value is None or isinstance(value, (str, bool, int, float)):
                return value
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                return value
            return {"unsupported_yaml": type(value).__name__}
        result.properties = {}
        name_value = None
        for key, value in values:
            converted = json_value(value)
            if isinstance(converted, dict) and "unsupported_yaml" in converted:
                result.unsupported_keys.add(key)
            if key not in result.duplicate_keys:
                result.properties[key] = converted
            if key == "name":
                name_value = converted
        identity = {"engagements", "id", "name"} | ({"client"} if result.kind == "project" else set())
        for key in result.unsupported_keys | result.duplicate_keys:
            defect = "duplicate_key" if key in result.duplicate_keys else "unsupported_yaml"
            if key in identity:
                result.defects.add(defect)
                result.properties[key] = None
            else:
                result.property_defects[key] = defect

        for key in identity - {"engagements"}:
            if (key not in result.properties or result.properties[key] is None) and (
                key not in result.unsupported_keys and key not in result.duplicate_keys
            ):
                result.defects.add("missing_field")
        # A field that is present with the wrong YAML type is unreadable, not valid:
        # a project's `client: true` or a `name: 123` would otherwise read as null
        # while the record still counted as clean. An identity field escalates like
        # the other identity defects; an optional field keeps a targeted defect.
        for key, wanted in (("name", str), ("client", str), ("status", str), ("aliases", list)):
            if key == "client" and result.kind != "project":
                continue
            value = result.properties.get(key)
            if value is None or isinstance(value, wanted):
                continue
            if key in result.unsupported_keys or key in result.duplicate_keys:
                continue
            if key in identity:
                result.defects.add("invalid_type")
                result.mistyped_keys.add(key)
                result.properties[key] = None
            else:
                result.property_defects[key] = "invalid_type"
        # A name that is *present* but not a string is not the same as a name that is
        # missing: find lists the record as invalid instead of only counting it. The
        # decision is about the value's type, not which defect code it produced, because
        # an unreadable mapping, list or non-finite float keeps its own
        # `unsupported_yaml` precedence over `invalid_type`, and because another field's
        # defect says nothing about the name. A name that is a string but could not be
        # read (a quoted key, a block scalar, a tag) has no usable value and stays
        # counted, like a missing or `null` one; so does a duplicated name.
        result.name_unreadable = (name_value is not None and not isinstance(name_value, str)
                                  and "name" not in result.duplicate_keys)
        id_values = [value for key, value in values if key == "id"]
        if len(id_values) == 1 and (not isinstance(id_values[0], str) or
                                    not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", id_values[0])):
            result.defects.add("invalid_id")
        result.id = result.properties.get("id")
        result.name = result.properties.get("name")
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        fallback_block = block.replace("\r\n", "\n")
        marker = re.search(r"(?m)^engagements:[ \t]*(client|project)[ \t]*$", fallback_block)
        if marker is None:
            return result
        result.is_record = True
        result.kind = marker.group(1)
        result.defects.add("yaml_parse")
        result.properties = None
        for key in ("id", "name"):
            match = re.search(rf"(?m)^{key}:[ \t]*(\S.*)$", fallback_block)
            if match:
                setattr(result, key, match.group(1).strip().strip("\"'"))
    result.global_defects = result.defects
    fence = None
    active = None
    for i in range(closing + 1, len(lines)):
        line = lines[i]
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            symbol = marker.group(1)
            if fence is None and (symbol[0] != "`" or "`" not in marker.group(2)):
                fence = symbol
                continue
            if (fence is not None and symbol[0] == fence[0] and len(symbol) >= len(fence)
                    and re.fullmatch(r"[ \t]*", marker.group(2))):
                fence = None
                continue
        if fence is not None:
            continue
        heading = re.match(r"^ {0,3}(#{1,2})(?:[ \t]+(.*)|[ \t]*)$", line)
        if not heading:
            continue
        if active is not None:
            active.end = offsets[i]
            active = None
        if heading.group(1) == "##":
            title = re.sub(r"(?:^|[ \t]+)#+[ \t]*$", "", heading.group(2) or "").strip()
            active = Section(title, section_key(title), offsets[i], offsets[i] + len(raw_lines[i]), len(data))
            result.sections.append(active)
    seen_keys = set()
    for section in result.sections:
        # One pass over the headings: recounting the keys per heading made a record
        # with many sections quadratic, and discovery parses every record in full.
        if section.key in seen_keys:
            result.section_defects[section.key] = "duplicate_section"
        seen_keys.add(section.key)
    # Discovery adds library-wide duplicate_id and unknown_client defects after
    # comparing records; this parser intentionally has no library context.
    return result


def footprint(data, op):
    record = parse(data)
    if op["op"] == "set_property":
        spans = record.property_spans.get(op["key"], [])
        if not spans:
            return None
        return b"".join(data[start:end] for start, end in spans).decode("utf-8").replace("\r\n", "\n")
    matches = [s for s in record.sections if s.key == section_key(op["section"])]
    if not matches:
        return None
    section = matches[0]
    value = data[section.start:section.end].decode("utf-8").replace("\r\n", "\n")
    return re.sub(r"(?:\n[ \t]*)+\Z", "", value)


def _render_scalar(value):
    if isinstance(value, (date, datetime)):
        # SPEC 4.2/4.3 keep ``updated`` a date, and the create template writes
        # it plain. An ISO date is always a safe plain YAML scalar, so the
        # plugin's own edit must not turn it into a quoted string.
        return value.isoformat()
    try:
        if yaml.safe_load(f"key: {value}") == {"key": value}:
            return value
    except Exception:
        pass
    return json.dumps(value, ensure_ascii=False)


def _render_list(values):
    result = []
    for value in values:
        try:
            plain = yaml.safe_load(f"key: [{value}]") == {"key": [value]}
        except Exception:
            plain = False
        result.append(value if plain else json.dumps(value, ensure_ascii=False))
    return "[" + ", ".join(result) + "]"


def _render(kind, id, name, client, *, clock, profile, source, properties, sections):
    properties = properties or {}
    sections = sections or []
    date = clock().date().isoformat()
    lines = ["---", f"engagements: {kind}", f"id: {_render_scalar(id)}"]
    if kind == "project":
        lines.append(f"client: {_render_scalar(client)}")
    lines.extend([f"name: {_render_scalar(name)}",
                  f"aliases: {_render_list(properties.get('aliases', []))}",
                  f"status: {_render_scalar(properties.get('status', 'active'))}"])
    if kind == "project":
        lines.append(f"locations: {_render_list(properties.get('locations', []))}")
    lines.extend([f"updated: {date}", "---", ""])
    if kind == "client":
        lines.extend([f"# {name}", ""])
        headings = ["Summary", "Current work", "Contacts", "Communication preferences",
                    "Locations", "Systems of record", "History", "Unconfirmed"]
    else:
        headings = ["Purpose", "Notes"]
    supplied = {section_key(item["section"]): item["body"] for item in sections}
    template_keys = {section_key(title) for title in headings}
    titles = headings + [item["section"].strip() for item in sections
                         if section_key(item["section"]) not in template_keys]
    expected = [section_key(title) for title in titles] + ["change log"]
    for title in titles:
        body = supplied.get(section_key(title), "").replace("\r\n", "\n")
        body = re.sub(r"(?:\n[ \t]*)+\Z", "", body)
        if not body.strip(" \t\n"):
            body = ""
        lines.append(f"## {title}")
        if body:
            lines.append(body)
        lines.append("")
    lines.extend(["## Change log", f"- {date} {profile or 'default'}: created {kind} (source: {source})", ""])
    data = "\n".join(lines).encode("utf-8")
    # A title can change identity once rendered as a heading: Markdown drops a
    # closing ``###`` run, so ``Change log ###`` would become a second
    # plugin-owned heading. Refuse any render whose sections do not round-trip.
    rendered = parse(data)
    if [section.key for section in rendered.sections] != expected or rendered.section_defects:
        _invalid_result("sections", "structure_changed")
    return data


def render_client(id, name, *, clock, profile=None, source="conversation", properties=None, sections=None):
    return _render("client", id, name, None, clock=clock, profile=profile, source=source,
                   properties=properties, sections=sections)


def render_project(id, name, client, *, clock, profile=None, source="conversation", properties=None, sections=None):
    return _render("project", id, name, client, clock=clock, profile=profile, source=source,
                   properties=properties, sections=sections)


def _malformed(defects):
    raise EngagementsError("malformed", "Record has defects; run doctor.",
                           {"defects": [{"kind": defect} for defect in sorted(defects)]})


def _frontmatter_end(record, data):
    """Offset where a new property is inserted: the closing ``---`` line."""
    end = record.frontmatter_span[1]
    for line_end in (end, end - len(record.eol)):
        if data[line_end - 3:line_end] == b"---":
            return line_end - 3
    return end


def _body_line_spans(data, section):
    """The section body as ``(offset, line bytes)`` pairs, file order."""
    spans, position = [], section.body_start
    for line in _file_lines(data[section.body_start:section.end]):
        spans.append((position, line))
        position += len(line)
    return spans


def _append_offset(data, section):
    """Where ``append_to_section`` inserts: after the last non-blank body line."""
    spans = _body_line_spans(data, section)
    index = len(spans)
    while index and not _line_content(spans[index - 1][1]).strip():
        index -= 1
    return section.body_start + sum(len(line) for _, line in spans[:index])


def _authorized_span(record, data, op):
    """The exact interval one op may change, derived from the pre-edit structure.

    ``start == end`` is a pure insertion. The caller invokes this only after the
    helper succeeded, so the structure read here is the one the op edited; a
    mismatch means a helper reached bytes the contract does not authorize.
    """
    if op["op"] == "set_property":
        spans = record.property_spans.get(op["key"], [])
        if spans:
            return spans[0]
        offset = _frontmatter_end(record, data)
        return offset, offset
    key = section_key(op["section"])
    found = next((section for section in record.sections if section.key == key), None)
    if found is None:
        log = next((section for section in record.sections if section.key == "change log"), None)
        if log is None:
            _structure_changed()
        return log.start, log.start
    if op["op"] == "replace_section":
        return found.start, found.end
    if op["op"] == "append_to_section":
        offset = _append_offset(data, found)
        return offset, offset
    hits = [(offset, line) for offset, line in _body_line_spans(data, found)
            if op["match"] in _line_content(line).decode("utf-8")]
    if len(hits) != 1:
        _structure_changed()
    offset, line = hits[0]
    return offset, offset + len(line)


def _set_property(data, key, value):
    record = parse(data)
    if key in record.property_defects:
        _malformed({record.property_defects[key]})
    spans = record.property_spans.get(key, [])
    eol = record.eol
    if isinstance(value, list):
        existing = data[slice(*spans[0])].decode('utf-8') if spans else ''
        block = re.search(r'(?m)^([ \t]*-[ \t]+)', existing)
        if value and block:
            prefix = block.group(1)
            items = []
            for item in value:
                try:
                    plain = yaml.safe_load(f'key:\n{prefix}{item}') == {'key': [item]}
                except Exception:
                    plain = False
                items.append(prefix + (item if plain else json.dumps(item, ensure_ascii=False)))
            replacement = (key + ':' + eol.decode() + eol.decode().join(items)).encode()
        else:
            replacement = f'{key}: {_render_list(value)}'.encode()
    else:
        replacement = f'{key}: {_render_scalar(value)}'.encode()
    if spans:
        start, end = spans[0]
        if data[start:end].endswith(eol):
            replacement += eol
    else:
        start = end = _frontmatter_end(record, data)
        replacement += eol
    return data[:start] + replacement + data[end:]


def _heading_key(title):
    """The section key the parser assigns to a written ``## <title>`` line."""
    return section_key(re.sub(r"(?:^|[ \t]+)#+[ \t]*$", "", title.strip()).strip())


def _section(data, title, action, content='', match=None, index=0):
    record = parse(data)
    key = section_key(title)
    if key in record.section_defects:
        _malformed({record.section_defects[key]})
    found = next((s for s in record.sections if s.key == key), None)
    if found is None:
        if action in ('replace_line', 'remove_line'):
            _invalid(index, 'section', 'section_missing')
        log = next((s for s in record.sections if s.key == 'change log'), None)
        if log is None or _heading_key(title) != key:
            # The new heading must re-parse as the requested key; a closing
            # ``#`` run would otherwise change its identity once written.
            _invalid(index, 'section', 'structure_changed')
        eol = record.eol
        body = re.sub(r'(?:\n[ \t]*)+\Z', '', content.replace('\r\n', '\n'))
        insertion = ('## ' + title.strip() + '\n' + (body + '\n' if body else '') + '\n').replace('\n', eol.decode()).encode()
        return data[:log.start] + insertion + data[log.start:], title.strip(), True
    eol = record.eol
    body = data[found.body_start:found.end]
    content = re.sub(r'(?:\n[ \t]*)+\Z', '', content.replace('\r\n', '\n'))
    content_bytes = content.replace('\n', eol.decode()).encode()
    if action == 'replace_section':
        heading = data[found.start:found.body_start]
        if not heading.endswith(eol):
            heading += eol
        replacement = heading + (content_bytes + eol if content_bytes else b'')
        if found.end < len(data):
            replacement += eol
        return data[:found.start] + replacement + data[found.end:], found.title, False
    if action == 'append_to_section':
        lines = _file_lines(body)
        i = len(lines)
        while i and not _line_content(lines[i - 1]).strip():
            i -= 1
        before = b''.join(lines[:i])
        after = b''.join(lines[i:])
        if before and not before.endswith(eol):
            before += eol
        prefix = data[:found.body_start]
        if not before and not prefix.endswith(eol):
            # A heading-only section at end of file still needs its line break.
            prefix += eol
        insertion = content_bytes + (eol if after or found.end < len(data) or record.final_newline else b'')
        return prefix + before + insertion + after + data[found.end:], found.title, False
    lines = _file_lines(body)
    hits = [i for i, line in enumerate(lines) if match in _line_content(line).decode('utf-8')]
    if len(hits) != 1:
        raise EngagementsError('invalid_op', 'Invalid operation.',
                               {'op_index': index, 'field': 'match', 'reason': 'match_count', 'count': len(hits)})
    i = hits[0]
    if action == 'remove_line':
        del lines[i]
    else:
        lines[i] = content_bytes + (eol if lines[i].endswith(eol) else b'')
    return data[:found.body_start] + b''.join(lines) + data[found.end:], found.title, False


def _structure_changed():
    _invalid_result("ops", "structure_changed")


def _check_structure(base, final, expected, touched, guard):
    """Post-apply structural check (TOOLS.md stage 7).

    Re-parses the result and enforces the documented invariants: the
    frontmatter still parses, the ordered section key list is exactly the
    expected one, every heading line that existed before is unchanged, every
    property span no op targeted still carries the same bytes, and every byte
    outside the spans the ops were authorised to edit is identical to the
    input.  The guard is what catches a splice that reached neighbouring
    frontmatter bytes, level-1 content or an untargeted section body.
    """
    if not final.is_record or final.defects or not final.frontmatter_span:
        _structure_changed()
    if [section.key for section in final.sections] != expected:
        _structure_changed()
    headings = [(section.key, _line_content(final.data[section.start:section.body_start]))
                for section in final.sections]
    position = 0
    for section in base.sections:
        heading = (section.key, _line_content(base.data[section.start:section.body_start]))
        while position < len(headings) and headings[position] != heading:
            position += 1
        if position == len(headings):
            _structure_changed()
        position += 1
    for key, spans in base.property_spans.items():
        if key in touched or key == "updated":
            continue
        before = [base.data[start:end] for start, end in spans]
        after = [final.data[start:end] for start, end in final.property_spans.get(key, [])]
        if before != after:
            _structure_changed()
    guard.verify(final.data)


def apply_ops(data, kind, ops, *, clock, profile, source, library_defects=(), client_exists):
    """Apply validated operations to an in-memory record, never touching disk.

    ``library_defects`` carries the library-wide facts the caller computed
    against these bytes (``duplicate_id``, ``unknown_client``); anything else is
    a caller bug, like an unresolved ``kind``, and raises ``ValueError``.
    """
    external = set(library_defects)
    if external - {"duplicate_id", "unknown_client"}:
        raise ValueError("Invalid library defect")
    if kind not in ("client", "project"):
        raise ValueError("kind must be 'client' or 'project'")
    # Stage 1 must precede every record-derived failure (TOOLS.md precedence).
    _validate_shapes(ops)
    record = parse(data)
    if not record.is_record:
        raise ValueError("data is not an engagements record")
    defects = set(record.global_defects) | external
    # spec C5: only a request made entirely of client repairs may reach the record.
    repair = (defects == {"unknown_client"} and kind == "project" and
              all(op["op"] == "set_property" and op["key"] == "client"
                  and client_exists(op["value"]) for op in ops))
    if defects and not repair:
        _malformed(defects)
    if record.kind != kind:
        raise ValueError("kind does not match the record")
    _validate_values(kind, ops, client_exists=client_exists)
    if record.property_defects.get("updated"):
        _malformed({record.property_defects["updated"]})
    if "change log" in record.section_defects:
        _malformed({"duplicate_section"})
    expected = [section.key for section in record.sections]
    touched = {op["key"] for op in ops if op["op"] == "set_property"}
    eol = record.eol
    original_final = record.final_newline
    guard = _ByteGuard(data)
    if "change log" not in expected:
        before = data
        if not data.endswith(eol):
            data += eol
        if _line_content(_file_lines(data)[-1]).strip():
            data += eol
        data += b"## Change log" + eol
        guard.step(before, data, len(before), len(before))
        expected.append("change log")
        # Only the frontmatter boundary is checked here, because the helpers
        # below need a parseable frontmatter. The 1 MiB limit applies to the
        # resulting file (TOOLS.md:98) and is enforced once, on the final bytes,
        # so a shrinking edit to a full-size record still succeeds.
        if not parse(data).frontmatter_span:
            _invalid_result("ops", "too_large")
    summaries = []
    for index, op in enumerate(ops):
        action = op["op"]
        if action == "set_property":
            key = op["key"]
            value = op.get("value", op.get("values"))
            if key == "aliases":
                seen = set()
                unique = []
                for item in value:
                    normalized = normalize_text(item)
                    if normalized not in seen:
                        seen.add(normalized)
                        unique.append(item)
                value = unique
            before = data
            data = _set_property(data, key, value)
            start, end = _authorized_span(parse(before), before, op)
            guard.step(before, data, start, end)
            if not parse(data).frontmatter_span:
                _invalid_result("ops", "too_large")
            summaries.append(f"set {key}")
        else:
            before = data
            data, title, created = _section(before, op["section"], action, op.get("body", op.get("text", "")), op.get("match"), index)
            start, end = _authorized_span(parse(before), before, op)
            guard.step(before, data, start, end)
            if created:
                expected.insert(expected.index("change log"), section_key(op["section"]))
            prefix = {"replace_section": "replace section", "append_to_section": "append to",
                      "replace_line": "replace line in", "remove_line": "remove line from"}[action]
            summaries.append(f"{prefix} {title}")
    stamp = clock().date()
    date = stamp.isoformat()
    before = data
    data = _set_property(data, "updated", stamp)
    record_before = parse(before)
    updated_spans = record_before.property_spans.get("updated", [])
    if updated_spans:
        start, end = updated_spans[0]
    else:
        start = end = _frontmatter_end(record_before, before)
    guard.step(before, data, start, end)
    if not parse(data).frontmatter_span:
        _invalid_result("ops", "too_large")
    line = f'- {date} {profile or "default"}: {"; ".join(summaries)} (source: {source})'
    before = data
    data, _, _ = _section(data, "Change log", "append_to_section", line)
    log = next(section for section in parse(before).sections if section.key == "change log")
    offset = _append_offset(before, log)
    guard.step(before, data, offset, offset)
    if original_final and not data.endswith(eol):
        before = data
        data += eol
        guard.step(before, data, len(before), len(before))
    if not original_final and data.endswith(eol):
        before = data
        data = data[:-len(eol)]
        guard.step(before, data, len(before) - len(eol), len(before))
    if len(data) > 1048576:
        _invalid_result("ops", "too_large")
    final = parse(data)
    if not final.frontmatter_span or final.frontmatter_span[1] > 65536:
        _invalid_result("ops", "too_large")
    _check_structure(record, final, expected, touched, guard)
    return data
