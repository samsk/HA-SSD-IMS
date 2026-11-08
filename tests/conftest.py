"""Shared pytest configuration.

Home Assistant's test harness (the `pytest-homeassistant` package) is a
pytest plugin that loads automatically, so the `hass`, `recorder_mock` and
related fixtures need no import here. Its `hass` fixture enables custom
integrations by default.

Tests that need the recorder (this integration declares "recorder" as a
manifest dependency) request `recorder_mock` before anything touches
`hass`, because the recorder's setup has to run first.
"""
