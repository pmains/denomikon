"""Tests for Community Action Commission (CAC) table parsing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tiers import integration_test


_TEST_HTML = """<html><body>
<table border="1" cellspacing="0" cellpadding="0" style="width: 100%;">
<thead><tr><th scope="col">Item</th><th scope="col">Agenda Item</th><th scope="col">Presenter</th></tr></thead>
<tbody>
<tr><td>1.</td><td>Call to Order</td><td>Danielle Olaya</td></tr>
<tr><td>2.</td><td>Roll Call</td><td>Danielle Olaya</td></tr>
<tr><td>3.</td><td>Approve Bylaws</td><td>Leilani Tetteh</td></tr>
</tbody></table>
</body></html>"""


@integration_test
def test_cac_parse_items():
    """CAC table parsing should separate title from presenter."""
    from scraper.agendacenter import _parse_cac_table

    items = _parse_cac_table(_TEST_HTML, "https://example.com")
    assert len(items) == 3, f"Expected 3 items, got {len(items)}"

    # Item 1: Call to Order
    assert items[0]["agenda_item_number"] == "1"
    assert items[0]["agenda_item_title"] == "Call to Order"
    assert "Presented by: Danielle Olaya" in items[0]["agenda_item_text"]

    # Item 3: Approve Bylaws - Leilani Tetteh
    assert items[2]["agenda_item_number"] == "3"
    assert items[2]["agenda_item_title"] == "Approve Bylaws"
    assert "Presented by: Leilani Tetteh" in items[2]["agenda_item_text"]

    # Source body should be mcacc
    assert items[0]["source_body"] == "mcacc"


@integration_test
def test_cac_no_table():
    """Non-table HTML should return empty."""
    from scraper.agendacenter import _parse_cac_table

    items = _parse_cac_table("<html><body><p>No table here</p></body></html>", "https://example.com")
    assert items == []


@integration_test
def test_cac_wrong_headers():
    """Table without CAC headers should return empty."""
    from scraper.agendacenter import _parse_cac_table

    html = """<html><body>
    <table>
    <tr><th>Date</th><th>Topic</th></tr>
    <tr><td>1.</td><td>Something</td></tr>
    </table></body></html>"""
    items = _parse_cac_table(html, "https://example.com")
    assert items == []
