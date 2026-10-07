"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example News</title>
    <link>https://example.com</link>
    <language>en</language>
    <item>
      <title>Earthquake strikes Japan killing 12</title>
      <link>https://example.com/story?id=123&amp;utm_source=x</link>
      <description>&lt;p&gt;A magnitude 6.8 earthquake hit Japan.&lt;/p&gt;</description>
      <pubDate>Mon, 06 Oct 2025 09:00:00 GMT</pubDate>
      <author>newsroom@example.com</author>
    </item>
    <item>
      <title>Twelve killed after earthquake hits Japan</title>
      <link>https://example.com/other</link>
      <description>A second report.</description>
      <pubDate>Mon, 06 Oct 2025 09:10:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


@pytest.fixture
def sample_rss_bytes() -> bytes:
    return SAMPLE_RSS.encode("utf-8")
