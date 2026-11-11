"""The model-facing tool schemas and descriptions (TOOLS.md), as data.

Copied verbatim from ``docs/TOOLS.md``: the JSON Schema blocks, the per-op validation
schemas and the description table. ``tests/test_tools_schema.py`` re-parses the
document and fails if this module drifts from it. No Hermes imports.
"""

import json

DESCRIPTIONS = {
    'engagements_find': (
        'Find a client or project engagement by name or alias. Returns ids, names, status and file paths, never record contents. Use before creating a record, to avoid duplicates.'
    ),
    'engagements_bind': (
        'Bind this chat to a client or project engagement. Returns the client\'s summary, contacts, locations and active projects. Use mode "off" to unbind or "auto" to resume folder-based binding.'
    ),
    'engagements_context': (
        'Read a client or project record and its locations. With no id it reads the bound record. Returns properties, sections and the rev needed for engagements_update.'
    ),
    'engagements_create': (
        'Create a new client or project engagement record. Run engagements_find first, show the user the draft, and save only what they confirmed or marked as assumed.'
    ),
    'engagements_update': (
        'Update a client or project record with targeted edits. Pass the rev from your last read as base_rev. Never rewrite a whole record to change one line.'
    ),
}

_PARAMETERS = {
    "engagements_find": r"""
{
  "type": "object",
  "properties": {
    "query": {"type": "string", "maxLength": 200, "description": "Text to match against id, name and aliases. Empty string lists everything."},
    "kind": {"type": "string", "enum": ["client", "project"]},
    "include_inactive": {"type": "boolean", "default": false},
    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    "offset": {"type": "integer", "minimum": 0, "default": 0}
  },
  "required": ["query"],
  "additionalProperties": false
}
""",
    "engagements_bind": r"""
{
  "type": "object",
  "properties": {
    "client": {"type": "string", "maxLength": 200, "description": "Client id, or exact name or alias."},
    "project": {"type": "string", "maxLength": 200, "description": "Project id, or exact name or alias. Implies its client."},
    "mode": {"type": "string", "enum": ["explicit", "off", "auto"], "description": "Omit to infer: explicit when client or project is given, otherwise off."}
  },
  "additionalProperties": false
}
""",
    "engagements_context": r"""
{
  "type": "object",
  "properties": {
    "id": {"type": "string", "maxLength": 200, "description": "Record id, or exact name or alias."},
    "path": {"type": "string", "maxLength": 1024, "description": "Record path, relative to the library root or absolute inside it."},
    "kind": {"type": "string", "enum": ["client", "project"], "default": "client", "description": "With no id or path, which bound record to read."},
    "sections": {"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 50, "description": "Section titles to return. Omit for all; empty list for properties only."},
    "offset": {"type": "integer", "minimum": 0, "default": 0},
    "rev": {"type": "string", "pattern": "^[0-9a-f]{16}$", "description": "Required when offset > 0: the rev from the previous page."}
  },
  "additionalProperties": false
}
""",
    "engagements_create": r"""
{
  "type": "object",
  "properties": {
    "kind": {"type": "string", "enum": ["client", "project"]},
    "name": {"type": "string", "minLength": 1, "maxLength": 200},
    "client": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{1,62}$", "description": "Required for projects: the owning client's id."},
    "properties": {
      "type": "object",
      "properties": {
        "aliases": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 200}, "maxItems": 20},
        "status": {"type": "string", "enum": ["active", "paused", "former", "historical"]},
        "locations": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1024}, "maxItems": 50}
      },
      "additionalProperties": false
    },
    "sections": {
      "type": "array", "maxItems": 30,
      "items": {
        "type": "object",
        "properties": {"section": {"type": "string", "minLength": 1, "maxLength": 200}, "body": {"type": "string", "maxLength": 20000}},
        "required": ["section", "body"],
        "additionalProperties": false
      }
    },
    "source": {"type": "string", "minLength": 1, "maxLength": 200, "default": "conversation"},
    "allow_similar": {"type": "boolean", "default": false}
  },
  "required": ["kind", "name"],
  "additionalProperties": false
}
""",
    "engagements_update": r"""
{
  "type": "object",
  "properties": {
    "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{1,62}$"},
    "base_rev": {"type": "string", "pattern": "^[0-9a-f]{16}$", "description": "The rev from your last read of this record."},
    "source": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Where the fact came from, for the Change log. One line."},
    "ops": {
      "type": "array", "minItems": 1, "maxItems": 50,
      "items": {
        "type": "object",
        "properties": {
          "op": {"type": "string", "enum": ["set_property", "replace_section", "append_to_section", "replace_line", "remove_line"]},
          "key": {"type": "string", "enum": ["name", "aliases", "status", "client", "locations"]},
          "value": {"type": "string", "maxLength": 200, "description": "For key name, status or client."},
          "values": {"type": "array", "items": {"type": "string", "maxLength": 1024}, "maxItems": 50, "description": "For key aliases or locations."},
          "section": {"type": "string", "minLength": 1, "maxLength": 200},
          "body": {"type": "string", "maxLength": 20000},
          "text": {"type": "string", "maxLength": 20000},
          "match": {"type": "string", "minLength": 1, "maxLength": 1000}
        },
        "required": ["op"],
        "additionalProperties": false
      }
    }
  },
  "required": ["id", "base_rev", "source", "ops"],
  "additionalProperties": false
}
""",
}

_OP_SCHEMAS = r"""
{
  "set_property_scalar": {
    "type": "object",
    "properties": {"op": {"const": "set_property"}, "key": {"enum": ["name", "status", "client"]}, "value": {"type": "string", "minLength": 1, "maxLength": 200}},
    "required": ["op", "key", "value"], "additionalProperties": false
  },
  "set_property_list": {
    "type": "object",
    "properties": {"op": {"const": "set_property"}, "key": {"enum": ["aliases", "locations"]}, "values": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1024}, "maxItems": 50}},
    "required": ["op", "key", "values"], "additionalProperties": false
  },
  "replace_section": {
    "type": "object",
    "properties": {"op": {"const": "replace_section"}, "section": {"type": "string", "minLength": 1, "maxLength": 200}, "body": {"type": "string", "maxLength": 20000}},
    "required": ["op", "section", "body"], "additionalProperties": false
  },
  "append_to_section": {
    "type": "object",
    "properties": {"op": {"const": "append_to_section"}, "section": {"type": "string", "minLength": 1, "maxLength": 200}, "text": {"type": "string", "minLength": 1, "maxLength": 20000}},
    "required": ["op", "section", "text"], "additionalProperties": false
  },
  "replace_line": {
    "type": "object",
    "properties": {"op": {"const": "replace_line"}, "section": {"type": "string", "minLength": 1, "maxLength": 200}, "match": {"type": "string", "minLength": 1, "maxLength": 1000}, "text": {"type": "string", "minLength": 1, "maxLength": 1000}},
    "required": ["op", "section", "match", "text"], "additionalProperties": false
  },
  "remove_line": {
    "type": "object",
    "properties": {"op": {"const": "remove_line"}, "section": {"type": "string", "minLength": 1, "maxLength": 200}, "match": {"type": "string", "minLength": 1, "maxLength": 1000}},
    "required": ["op", "section", "match"], "additionalProperties": false
  }
}
"""

PARAMETERS = {name: json.loads(text) for name, text in _PARAMETERS.items()}
OP_SCHEMAS = json.loads(_OP_SCHEMAS)
