"""HTTP layer over the globalinsight backend.

Thin by design: routes translate HTTP to existing backend calls and serialise
the results. No domain logic lives here - anything that looks like domain logic
belongs in globalinsight/, and is recorded as a backend request in
docs/UI-SPEC.md section 17 when it is missing.
"""
