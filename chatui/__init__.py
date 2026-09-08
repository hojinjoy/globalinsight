"""Streamlit chat UI for the GlobalInsight advisor brief.

Deliberately separate from the ``globalinsight`` package, which is the backend
and is owned elsewhere. Everything in here reads the backend's public API
(pipeline.wave2 / wave3 / build_synthesis_context, synthesize.generate_brief)
and adds nothing to it.

The one exception is ``chatui.qa``, which implements follow-up question
answering because the backend does not expose an entry point for it yet. See
that module's docstring - it should move into globalinsight.synthesize.
"""
