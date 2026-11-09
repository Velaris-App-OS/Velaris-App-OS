"""Form Builder definitions, shared by every service that accepts a form.

`validate` is the one server-side check of submitted form values. Studio's
`studio/src/modules/form-builder/validate.ts` mirrors it; both run the same
case table, `validation_cases.json`, so they cannot drift apart unnoticed.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from velaris_shared.forms.validate import FieldError, field_key_of, form_keys, validate, visible_fields
from velaris_shared.forms.portal import portal_definition, portal_keys

__all__ = ["FieldError", "field_key_of", "form_keys", "portal_definition", "portal_keys", "validate", "visible_fields"]
